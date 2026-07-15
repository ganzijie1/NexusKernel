from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from nexuskernel.errors import ProviderError, ProviderUnavailableError
from nexuskernel.kernel import ProviderState
from nexuskernel.providers import ProviderRegistry, ProviderSpec


class FakeProvider:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        start_error: Exception | None = None,
        healthy: bool = True,
        health_delay: float = 0,
        close_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.events = events
        self.start_error = start_error
        self.healthy = healthy
        self.health_delay = health_delay
        self.close_error = close_error

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.start_error is not None:
            raise self.start_error

    async def health_check(self) -> bool:
        if self.health_delay:
            await asyncio.sleep(self.health_delay)
        return self.healthy

    async def close(self) -> None:
        self.events.append(f"close:{self.name}")
        if self.close_error is not None:
            raise self.close_error


def _factory(provider: FakeProvider) -> Callable[[], FakeProvider]:
    return lambda: provider


@pytest.mark.asyncio
async def test_registry_initializes_sync_and_async_factories_in_order() -> None:
    events: list[str] = []
    first = FakeProvider("first", events)
    second = FakeProvider("second", events)

    async def make_second() -> FakeProvider:
        return second

    registry = ProviderRegistry()
    registry.register(ProviderSpec("first", _factory(first)))
    registry.register(ProviderSpec("second", make_second))

    await registry.initialize_all()

    assert events == ["start:first", "start:second"]
    assert registry.require("first") is first
    assert registry.get("second") is second
    assert [item["state"] for item in registry.statuses()] == ["ready", "ready"]


def test_registry_rejects_duplicate_names() -> None:
    registry = ProviderRegistry()
    registry.register(ProviderSpec("cache", lambda: object()))

    with pytest.raises(ProviderError, match="already registered"):
        registry.register(ProviderSpec("cache", lambda: object()))


@pytest.mark.asyncio
async def test_optional_provider_failure_degrades_without_leaking_exception() -> None:
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "optional",
            _factory(
                FakeProvider(
                    "optional", [], start_error=RuntimeError("password=secret-value")
                )
            ),
            required=False,
        )
    )

    await registry.initialize_all()

    assert registry.get("optional") is None
    status = registry.statuses()[0]
    assert status == {
        "name": "optional",
        "required": False,
        "state": "degraded",
        "error": "provider_initialization_failed",
    }
    assert "secret-value" not in repr(status)


@pytest.mark.asyncio
async def test_required_provider_failure_rolls_back_started_providers() -> None:
    events: list[str] = []
    registry = ProviderRegistry()
    registry.register(ProviderSpec("first", _factory(FakeProvider("first", events))))
    registry.register(
        ProviderSpec(
            "required",
            _factory(
                FakeProvider(
                    "required", events, start_error=RuntimeError("api-key=secret")
                )
            ),
        )
    )

    with pytest.raises(ProviderUnavailableError) as captured:
        await registry.initialize_all()

    assert "secret" not in str(captured.value)
    assert events == [
        "start:first",
        "start:required",
        "close:required",
        "close:first",
    ]
    assert registry.statuses()[0]["state"] == "stopped"
    assert registry.statuses()[1]["state"] == "failed"


@pytest.mark.asyncio
async def test_health_refresh_degrades_on_false_and_timeout() -> None:
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "unhealthy",
            _factory(FakeProvider("unhealthy", [], healthy=False)),
            required=False,
        )
    )
    registry.register(
        ProviderSpec(
            "slow",
            _factory(FakeProvider("slow", [], health_delay=0.05)),
            required=False,
            timeout_seconds=0.01,
        )
    )
    await registry.initialize_all()

    statuses = await registry.refresh_health()

    assert [item["state"] for item in statuses] == ["degraded", "degraded"]
    assert statuses[0]["error"] == "provider_unhealthy"
    assert statuses[1]["error"] == "provider_health_timeout"


@pytest.mark.asyncio
async def test_close_is_reverse_order_idempotent_and_aggregates_errors() -> None:
    events: list[str] = []
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "first",
            _factory(
                FakeProvider("first", events, close_error=RuntimeError("close secret"))
            ),
        )
    )
    registry.register(ProviderSpec("second", _factory(FakeProvider("second", events))))
    await registry.initialize_all()

    errors = await registry.close_all()
    second_errors = await registry.close_all()

    assert events == ["start:first", "start:second", "close:second", "close:first"]
    assert errors == ["provider_close_failed:first"]
    assert second_errors == []
    assert "secret" not in repr(errors)
    assert [item["state"] for item in registry.statuses()] == ["failed", "stopped"]


@pytest.mark.asyncio
async def test_health_check_propagates_cancellation() -> None:
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "slow",
            _factory(FakeProvider("slow", [], health_delay=60)),
            timeout_seconds=120,
        )
    )
    await registry.initialize_all()

    task = asyncio.create_task(registry.refresh_health())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_initialization_cancellation_rolls_back_resources() -> None:
    events: list[str] = []

    class BlockingProvider(FakeProvider):
        async def start(self) -> None:
            self.events.append(f"start:{self.name}")
            await asyncio.sleep(60)

    registry = ProviderRegistry()
    registry.register(ProviderSpec("first", _factory(FakeProvider("first", events))))
    registry.register(
        ProviderSpec(
            "blocking",
            _factory(BlockingProvider("blocking", events)),
            timeout_seconds=120,
        )
    )

    task = asyncio.create_task(registry.initialize_all())
    while "start:blocking" not in events:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "start:first",
        "start:blocking",
        "close:blocking",
        "close:first",
    ]
    assert [item["state"] for item in registry.statuses()] == ["stopped", "failed"]


def test_disabled_provider_is_not_created() -> None:
    calls: list[str] = []
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "disabled",
            lambda: calls.append("created"),
            enabled=False,
            required=False,
        )
    )

    assert calls == []
    assert registry.statuses() == [
        {
            "name": "disabled",
            "required": False,
            "state": ProviderState.DISABLED.value,
            "error": None,
        }
    ]


@pytest.mark.asyncio
async def test_require_rejects_unknown_and_unavailable_providers() -> None:
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "optional",
            _factory(FakeProvider("optional", [], start_error=RuntimeError("down"))),
            required=False,
        )
    )
    await registry.initialize_all()

    with pytest.raises(ProviderUnavailableError):
        registry.require("optional")
    with pytest.raises(ProviderUnavailableError):
        registry.require("missing")


@pytest.mark.parametrize("name", ["", " has-space", "UPPER", "slash/name"])
def test_provider_spec_requires_a_stable_identifier(name: str) -> None:
    with pytest.raises(ValueError):
        ProviderSpec(name, lambda: object())
