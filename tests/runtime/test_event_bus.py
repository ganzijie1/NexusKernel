from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus_kernel.runtime import Event, EventBus


@pytest.mark.asyncio
async def test_event_bus_preserves_subscription_order_and_unsubscribes() -> None:
    bus = EventBus()
    calls: list[tuple[str, Any]] = []

    async def first(event: Event) -> None:
        calls.append(("first", event.payload["value"]))

    def second(event: Event) -> None:
        calls.append(("second", event.payload["value"]))

    remove_first = bus.subscribe("created", first)
    bus.subscribe("created", second)

    assert await bus.publish(Event("created", {"value": 4})) == []
    remove_first()
    assert await bus.publish(Event("created", {"value": 5})) == []

    assert calls == [("first", 4), ("second", 4), ("second", 5)]


@pytest.mark.asyncio
async def test_event_bus_aggregates_safe_handler_errors() -> None:
    bus = EventBus()

    async def broken(event: Event) -> None:
        raise RuntimeError(f"Authorization: Bearer {event.payload['secret']}")

    bus.subscribe("created", broken)

    errors = await bus.publish(
        Event("created", {"secret": "must-not-leak"})  # security-test-fixture
    )

    assert errors == ["event_handler_failed:created:0"]
    assert "must-not-leak" not in repr(errors)


@pytest.mark.asyncio
async def test_event_bus_propagates_handler_cancellation() -> None:
    bus = EventBus()

    async def blocking(event: Event) -> None:
        await asyncio.sleep(60)

    bus.subscribe("blocking", blocking)
    task = asyncio.create_task(bus.publish(Event("blocking")))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("name", ["", " whitespace", "UPPER", "bad/name"])
def test_event_requires_a_stable_name(name: str) -> None:
    with pytest.raises(ValueError):
        Event(name)
