"""Small in-process event bus with deterministic, cancellation-safe delivery."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_.-]*", self.name) is None:
            raise ValueError("event name must be a stable lowercase identifier")


EventHandler: TypeAlias = Callable[[Event], None | Awaitable[None]]
Unsubscribe: TypeAlias = Callable[[], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_name: str, handler: EventHandler) -> Unsubscribe:
        Event(event_name)
        handlers = self._handlers.setdefault(event_name, [])
        handlers.append(handler)
        active = True

        def unsubscribe() -> None:
            nonlocal active
            if active:
                handlers.remove(handler)
                active = False

        return unsubscribe

    async def publish(self, event: Event) -> list[str]:
        errors: list[str] = []
        for index, handler in enumerate(tuple(self._handlers.get(event.name, ()))):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                errors.append(f"event_handler_failed:{event.name}:{index}")
        return errors
