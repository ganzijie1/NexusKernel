"""Bounded Google ADK session metadata and cleanup barriers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, Protocol

from nexus_kernel._validation import require_positive_finite_number
from nexus_kernel.ai.contracts import SessionKey
from nexus_kernel.ai.errors import AiProviderError


class AdkSessionLease:
    """Metadata lease held while one ADK session is in use."""

    __slots__ = (
        "_event_count",
        "_invalidated",
        "_remaining_events",
        "_requires_cleanup",
        "_reserve_cleanup",
        "_cleanup_reserved",
        "_touched",
        "cleanup_keys",
        "key",
        "reset_session",
    )

    def __init__(
        self,
        key: SessionKey,
        *,
        reset_session: bool,
        cleanup_keys: tuple[SessionKey, ...],
        remaining_events: int,
        reserve_cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.key = key
        self.reset_session = reset_session
        self.cleanup_keys = cleanup_keys
        self._remaining_events = remaining_events
        self._event_count = 0
        self._invalidated = False
        self._requires_cleanup = False
        self._reserve_cleanup = reserve_cleanup
        self._cleanup_reserved = False
        self._touched = False

    async def reserve_cleanup(self) -> None:
        """Reserve capacity for an atomic entry-to-tombstone transition."""
        if self._cleanup_reserved:
            return
        if self._reserve_cleanup is not None:
            await self._reserve_cleanup()
        self._cleanup_reserved = True

    def mark_touched(self) -> None:
        if not self._cleanup_reserved:
            raise RuntimeError("session cleanup capacity must be reserved first")
        self._touched = True

    def record_events(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise AiProviderError("Google ADK returned invalid event usage")
        self._event_count += count
        if self._event_count >= self._remaining_events:
            self._invalidated = True

    def invalidate(self) -> None:
        self._invalidated = True

    def mark_cleanup_required(self) -> None:
        """Tell the runtime that backend state must be removed for this key."""
        if not self._cleanup_reserved:
            raise RuntimeError("session cleanup capacity must be reserved first")
        self._requires_cleanup = True

    @property
    def invalidated(self) -> bool:
        return self._invalidated

    @property
    def touched(self) -> bool:
        return self._touched

    @property
    def requires_cleanup(self) -> bool:
        return self._requires_cleanup


class AdkSessionStore(Protocol):
    def lease(
        self, key: SessionKey, *, temporary: bool = False
    ) -> AsyncContextManager[AdkSessionLease]: ...

    async def complete_cleanup(self, key: SessionKey) -> None: ...

    async def pin(self, key: SessionKey) -> None: ...

    async def unpin(self, key: SessionKey) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _SessionEntry:
    last_used: float
    event_count: int = 0
    active: int = 0
    needs_reset: bool = True
    pin_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BoundedMemoryAdkSessionStore:
    """Bound entries and cleanup tombstones while isolating session locks."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 1800.0,
        max_entries: int = 256,
        max_events: int = 200,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        require_positive_finite_number(ttl_seconds, "session TTL")
        if max_entries < 1:
            raise ValueError("session capacity must be positive")
        if max_events < 1:
            raise ValueError("session event limit must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._max_events = max_events
        self._time_source = time_source
        self._entries: dict[SessionKey, _SessionEntry] = {}
        self._pending_cleanup: set[SessionKey] = set()
        self._cleanup_reservations: set[SessionKey] = set()
        self._condition = asyncio.Condition()
        self._closed = False

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def cleanup_size(self) -> int:
        return len(self._pending_cleanup)

    def lease(
        self, key: SessionKey, *, temporary: bool = False
    ) -> AsyncContextManager[AdkSessionLease]:
        return self._lease(key, temporary=temporary)

    @asynccontextmanager
    async def _lease(self, key: SessionKey, *, temporary: bool) -> Any:
        cleanup_keys: list[SessionKey] = []
        entry: _SessionEntry | None = None
        acquired = False
        lease: AdkSessionLease | None = None
        try:
            while lease is None:
                entry, created, newly_evicted = await self._reserve(key)
                cleanup_keys.extend(newly_evicted)
                await entry.lock.acquire()
                acquired = True
                async with self._condition:
                    if self._closed:
                        entry.active -= 1
                        entry.lock.release()
                        acquired = False
                        entry = None
                        self._condition.notify_all()
                        raise AiProviderError("Google ADK session store is closed")
                    if self._entries.get(key) is not entry:
                        entry.active -= 1
                        entry.lock.release()
                        acquired = False
                        entry = None
                        self._condition.notify_all()
                        continue
                    now = self._time_source()
                    if created or (
                        entry.pin_count == 0
                        and (
                            now - entry.last_used >= self._ttl_seconds
                            or entry.event_count >= self._max_events
                        )
                    ):
                        entry.event_count = 0
                        entry.needs_reset = True
                    lease = AdkSessionLease(
                        key,
                        reset_session=entry.needs_reset,
                        cleanup_keys=tuple(dict.fromkeys(cleanup_keys)),
                        remaining_events=self._max_events - entry.event_count,
                        reserve_cleanup=lambda: self._reserve_cleanup_slot(key, entry),
                    )
            yield lease
        finally:
            if entry is not None:
                await self._complete_lease(
                    key,
                    entry=entry,
                    lease=lease,
                    temporary=temporary,
                    acquired=acquired,
                )

    async def _complete_lease(
        self,
        key: SessionKey,
        *,
        entry: _SessionEntry,
        lease: AdkSessionLease | None,
        temporary: bool,
        acquired: bool,
    ) -> None:
        interrupted = False
        current = asyncio.current_task()
        condition_acquired = False
        while not condition_acquired:
            try:
                await self._condition.acquire()
            except asyncio.CancelledError:
                interrupted = True
                continue
            condition_acquired = True
        if current is not None and current.cancelling():
            interrupted = True
        try:
            if lease is not None:
                entry.event_count += lease._event_count
                if lease._touched:
                    entry.needs_reset = False
            entry.last_used = self._time_source()
            should_remove = lease is not None and (
                temporary or lease._invalidated or (interrupted and lease._touched)
            )
            entry.active -= 1
            if should_remove and entry.pin_count:
                raise AiProviderError("Google ADK pinned session cannot be removed")
            if should_remove and self._entries.get(key) is entry:
                if lease is not None and lease._touched and not self._closed:
                    if not lease._cleanup_reserved:
                        raise AiProviderError(
                            "Google ADK cleanup capacity was not reserved"
                        )
                    if len(self._pending_cleanup) >= self._max_entries:
                        raise AiProviderError(
                            "Google ADK cleanup capacity was exhausted"
                        )
                self._entries.pop(key, None)
                if lease is not None and lease._touched and not self._closed:
                    self._pending_cleanup.add(key)
                    lease.mark_cleanup_required()
        finally:
            self._cleanup_reservations.discard(key)
            if acquired:
                entry.lock.release()
            self._condition.notify_all()
            self._condition.release()
        if interrupted:
            raise asyncio.CancelledError

    async def _reserve_cleanup_slot(
        self, key: SessionKey, entry: _SessionEntry
    ) -> None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: (
                    self._closed
                    or self._entries.get(key) is not entry
                    or key in self._pending_cleanup
                    or key in self._cleanup_reservations
                    or len(self._pending_cleanup) + len(self._cleanup_reservations)
                    < self._max_entries
                )
            )
            if self._closed:
                raise AiProviderError("Google ADK session store is closed")
            if self._entries.get(key) is not entry or key in self._pending_cleanup:
                raise AiProviderError("Google ADK session lease is no longer available")
            self._cleanup_reservations.add(key)

    async def _reserve(
        self, key: SessionKey
    ) -> tuple[_SessionEntry, bool, list[SessionKey]]:
        evicted: list[SessionKey] = []
        async with self._condition:
            while True:
                if self._closed:
                    raise AiProviderError("Google ADK session store is closed")
                if key in self._pending_cleanup:
                    await self._condition.wait_for(
                        lambda: self._closed or key not in self._pending_cleanup
                    )
                    continue

                now = self._time_source()
                expired = self._expired_idle_keys(now, exclude=key)
                available_cleanup = self._max_entries - (
                    len(self._pending_cleanup) + len(self._cleanup_reservations)
                )
                if len(expired) > available_cleanup:
                    await self._condition.wait()
                    continue
                evicted.extend(self._remove_expired(expired))
                entry = self._entries.get(key)
                if entry is not None:
                    entry.active += 1
                    return entry, False, evicted
                if len(self._entries) < self._max_entries:
                    entry = _SessionEntry(last_used=now, active=1)
                    self._entries[key] = entry
                    return entry, True, evicted

                if (
                    len(self._pending_cleanup) + len(self._cleanup_reservations)
                    >= self._max_entries
                ):
                    await self._condition.wait()
                    continue
                candidate = self._lru_idle_key()
                if candidate is not None:
                    self._entries.pop(candidate)
                    self._pending_cleanup.add(candidate)
                    evicted.append(candidate)
                    continue
                await self._condition.wait()

    def _expired_idle_keys(
        self, now: float, *, exclude: SessionKey
    ) -> list[SessionKey]:
        return [
            key
            for key, entry in self._entries.items()
            if key != exclude
            and entry.active == 0
            and entry.pin_count == 0
            and now - entry.last_used >= self._ttl_seconds
        ]

    def _remove_expired(self, expired: list[SessionKey]) -> list[SessionKey]:
        for key in expired:
            self._entries.pop(key, None)
            self._pending_cleanup.add(key)
        return expired

    def _lru_idle_key(self) -> SessionKey | None:
        idle = [
            (entry.last_used, key)
            for key, entry in self._entries.items()
            if entry.active == 0 and entry.pin_count == 0
        ]
        return min(idle, default=(0.0, None), key=lambda item: item[0])[1]

    async def complete_cleanup(self, key: SessionKey) -> None:
        async with self._condition:
            self._pending_cleanup.discard(key)
            self._condition.notify_all()

    async def pin(self, key: SessionKey) -> None:
        async with self._condition:
            if self._closed:
                raise AiProviderError("Google ADK session store is closed")
            entry = self._entries.get(key)
            if entry is None:
                raise AiProviderError("Google ADK session is unavailable")
            entry.pin_count += 1

    async def unpin(self, key: SessionKey) -> None:
        async with self._condition:
            entry = self._entries.get(key)
            if entry is None or entry.pin_count < 1:
                raise AiProviderError("Google ADK session pin is unavailable")
            entry.pin_count -= 1
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._entries.clear()
            self._pending_cleanup.clear()
            self._cleanup_reservations.clear()
            self._condition.notify_all()
