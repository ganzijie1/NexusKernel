from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Callable

import pytest

from nexus_kernel.errors import ProviderError, ProviderUnavailableError
from nexus_kernel.providers import ProviderRegistry, ProviderSpec, ProviderState


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

    await registry.initialize_all()

    assert events == ["start:first", "start:second"]


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
        "state": "failed",
        "error": "provider_initialization_failed",
    }
    assert "secret-value" not in repr(status)


@pytest.mark.asyncio
async def test_async_factory_and_start_share_one_initialization_deadline() -> None:
    events: list[str] = []

    async def slow_factory() -> FakeProvider:
        await asyncio.sleep(0.03)
        return FakeProvider("slow", events)

    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "slow",
            slow_factory,
            required=False,
            timeout_seconds=0.01,
        )
    )

    await asyncio.wait_for(registry.initialize_all(), timeout=0.1)

    assert events == []
    assert registry.statuses()[0] == {
        "name": "slow",
        "required": False,
        "state": "failed",
        "error": "provider_initialization_timeout",
    }


@pytest.mark.asyncio
async def test_required_async_factory_timeout_rolls_back_active_providers() -> None:
    events: list[str] = []
    registry = ProviderRegistry()
    registry.register(ProviderSpec("first", _factory(FakeProvider("first", events))))

    async def blocked_factory() -> FakeProvider:
        await asyncio.sleep(60)
        return FakeProvider("blocked", events)

    registry.register(ProviderSpec("blocked", blocked_factory, timeout_seconds=0.01))

    with pytest.raises(ProviderUnavailableError):
        await registry.initialize_all()

    assert events == ["start:first", "close:first"]
    assert registry.statuses()[1]["error"] == "provider_initialization_timeout"


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
async def test_required_provider_failure_suppresses_sensitive_exception_chain() -> None:
    sensitive = "sdk-sensitive-credential"
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "required",
            _factory(
                FakeProvider(
                    "required",
                    [],
                    start_error=RuntimeError(sensitive),
                )
            ),
        )
    )

    with pytest.raises(ProviderUnavailableError) as captured:
        await registry.initialize_all()

    rendered = "".join(traceback.format_exception(captured.value))
    assert sensitive not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True


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

    with pytest.raises(
        ProviderUnavailableError, match="initialization previously failed"
    ):
        await registry.initialize_all()


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


def test_provider_spec_rejects_required_disabled_state() -> None:
    with pytest.raises(ValueError, match="required provider cannot be disabled"):
        ProviderSpec("required", lambda: object(), required=True, enabled=False)


@pytest.mark.asyncio
async def test_invalid_provider_candidate_is_closed_once() -> None:
    class InvalidProvider:
        def __init__(self) -> None:
            self.close_calls = 0

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            self.close_calls += 1

    candidate = InvalidProvider()
    registry = ProviderRegistry()
    registry.register(ProviderSpec("invalid", lambda: candidate, required=False))

    await registry.initialize_all()
    await registry.close_all()

    assert candidate.close_calls == 1
    assert registry.get("invalid") is None
    assert registry.statuses()[0]["error"] == "provider_initialization_failed"


@pytest.mark.asyncio
async def test_sync_lifecycle_candidate_is_rejected_without_invoking_malformed_close() -> (
    None
):
    class InvalidSyncProvider:
        def __init__(self) -> None:
            self.close_calls = 0

        def start(self) -> None:
            return None

        def health_check(self) -> bool:
            return True

        def close(self) -> None:
            self.close_calls += 1
            time.sleep(60)

    candidate = InvalidSyncProvider()
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "invalid_sync",
            lambda: candidate,  # type: ignore[arg-type]
            required=False,
            timeout_seconds=0.01,
        )
    )
    started = time.monotonic()

    await registry.initialize_all()

    elapsed = time.monotonic() - started
    assert elapsed < 0.15
    assert candidate.close_calls == 0
    assert registry.statuses()[0]["error"] == "provider_cleanup_unavailable"


@pytest.mark.asyncio
async def test_missing_candidate_close_reports_cleanup_unavailable() -> None:
    class MissingCloseProvider:
        async def start(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "missing_close",
            MissingCloseProvider,  # type: ignore[arg-type]
            required=False,
        )
    )

    await registry.initialize_all()

    assert registry.get("missing_close") is None
    assert registry.statuses()[0]["error"] == "provider_cleanup_unavailable"


@pytest.mark.asyncio
async def test_none_candidate_reports_cleanup_unavailable() -> None:
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "none_candidate",
            lambda: None,  # type: ignore[arg-type]
            required=False,
        )
    )

    await registry.initialize_all()

    assert registry.statuses()[0]["error"] == "provider_cleanup_unavailable"


@pytest.mark.asyncio
async def test_malformed_async_close_is_not_invoked() -> None:
    close_calls = 0

    class InvalidCloseProvider:
        async def start(self) -> None:
            return None

        async def health_check(self) -> bool:
            return True

        async def close(self, required_argument: object) -> None:
            nonlocal close_calls
            close_calls += 1

    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "invalid_close",
            InvalidCloseProvider,  # type: ignore[arg-type]
            required=False,
        )
    )

    await registry.initialize_all()

    assert close_calls == 0
    assert registry.statuses()[0]["error"] == "provider_cleanup_unavailable"


@pytest.mark.asyncio
async def test_start_timeout_closes_created_candidate_once() -> None:
    close_calls = 0

    class SlowProvider:
        async def start(self) -> None:
            await asyncio.sleep(60)

        async def health_check(self) -> bool:
            return True

        async def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    registry = ProviderRegistry()
    registry.register(
        ProviderSpec("slow_start", SlowProvider, required=False, timeout_seconds=0.01)
    )

    await registry.initialize_all()
    await registry.close_all()

    assert close_calls == 1
    assert registry.statuses()[0]["error"] == "provider_initialization_timeout"


@pytest.mark.asyncio
async def test_required_invalid_candidate_closes_before_active_rollback() -> None:
    events: list[str] = []

    class InvalidProvider:
        async def start(self) -> None:
            events.append("start:invalid")

        async def close(self) -> None:
            events.append("close:invalid")

    registry = ProviderRegistry()
    registry.register(ProviderSpec("first", _factory(FakeProvider("first", events))))
    registry.register(ProviderSpec("invalid", InvalidProvider))  # type: ignore[arg-type]

    with pytest.raises(ProviderUnavailableError):
        await registry.initialize_all()

    assert events == ["start:first", "close:invalid", "close:first"]


@pytest.mark.asyncio
async def test_candidate_close_cancel_does_not_mask_required_initialization_error() -> (
    None
):
    events: list[str] = []

    class CancelledCloseProvider(FakeProvider):
        async def close(self) -> None:
            events.append("close:required")
            raise asyncio.CancelledError

    registry = ProviderRegistry()
    registry.register(ProviderSpec("first", _factory(FakeProvider("first", events))))
    registry.register(
        ProviderSpec(
            "required",
            _factory(
                CancelledCloseProvider(
                    "required", events, start_error=RuntimeError("unavailable")
                )
            ),
        )
    )

    with pytest.raises(ProviderUnavailableError):
        await registry.initialize_all()

    assert events == [
        "start:first",
        "start:required",
        "close:required",
        "close:first",
    ]
    assert [item["state"] for item in registry.statuses()] == ["stopped", "failed"]
    assert registry.statuses()[1]["error"] == "provider_initialization_failed"


@pytest.mark.asyncio
async def test_active_close_cancel_does_not_stop_reverse_rollback() -> None:
    events: list[str] = []

    class CancelledCloseProvider(FakeProvider):
        async def close(self) -> None:
            events.append(f"close:{self.name}")
            raise asyncio.CancelledError

    registry = ProviderRegistry()
    registry.register(ProviderSpec("first", _factory(FakeProvider("first", events))))
    registry.register(
        ProviderSpec("second", _factory(CancelledCloseProvider("second", events)))
    )
    registry.register(
        ProviderSpec(
            "required",
            _factory(
                FakeProvider(
                    "required", events, start_error=RuntimeError("unavailable")
                )
            ),
        )
    )

    with pytest.raises(ProviderUnavailableError):
        await registry.initialize_all()

    assert events[-3:] == ["close:required", "close:second", "close:first"]
    assert [item["state"] for item in registry.statuses()] == [
        "stopped",
        "failed",
        "failed",
    ]


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_candidate_and_active_cleanup() -> None:
    events: list[str] = []
    start_entered = asyncio.Event()
    close_entered = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingProvider(FakeProvider):
        async def start(self) -> None:
            events.append("start:blocking")
            start_entered.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            events.append("close:blocking")
            close_entered.set()
            await release_close.wait()

    registry = ProviderRegistry()
    registry.register(ProviderSpec("first", _factory(FakeProvider("first", events))))
    registry.register(
        ProviderSpec(
            "blocking",
            _factory(BlockingProvider("blocking", events)),
            timeout_seconds=30,
        )
    )
    initializing = asyncio.create_task(registry.initialize_all())
    await start_entered.wait()

    initializing.cancel()
    await close_entered.wait()
    initializing.cancel()
    await asyncio.sleep(0)

    assert not initializing.done()
    assert registry.get("first") is not None

    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await initializing

    assert events[-2:] == ["close:blocking", "close:first"]
    assert registry.get("first") is None


@pytest.mark.asyncio
async def test_cancel_resistant_close_uses_a_bounded_independent_budget() -> None:
    release_close = asyncio.Event()

    class ResistantCloseProvider(FakeProvider):
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_close.wait()

    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "resistant",
            _factory(
                ResistantCloseProvider(
                    "resistant", [], start_error=RuntimeError("unavailable")
                )
            ),
            required=False,
            timeout_seconds=0.01,
        )
    )
    started = time.monotonic()

    await registry.initialize_all()

    assert time.monotonic() - started < 0.15
    assert registry.statuses()[0]["error"] == "provider_initialization_failed"
    await asyncio.sleep(0)
    release_close.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_invalid_provider_without_close_reports_cleanup_unavailable() -> None:
    class InvalidProvider:
        async def start(self) -> None:
            return None

    registry = ProviderRegistry()
    registry.register(
        ProviderSpec("invalid", InvalidProvider, required=False)  # type: ignore[arg-type]
    )

    await registry.initialize_all()

    assert registry.get("invalid") is None
    assert registry.statuses()[0]["error"] == "provider_cleanup_unavailable"


@pytest.mark.asyncio
async def test_required_failure_rejects_repeated_initialization() -> None:
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            "required",
            _factory(
                FakeProvider("required", [], start_error=RuntimeError("unavailable"))
            ),
        )
    )

    with pytest.raises(ProviderUnavailableError):
        await registry.initialize_all()
    with pytest.raises(
        ProviderUnavailableError, match="initialization previously failed"
    ):
        await registry.initialize_all()


def test_provider_states_define_the_public_lifecycle() -> None:
    assert [state.value for state in ProviderState] == [
        "registered",
        "starting",
        "ready",
        "degraded",
        "disabled",
        "stopped",
        "failed",
    ]
