"""Lifecycle supervision and observable status for cooperative workers."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from nexus_kernel._validation import require_positive_finite_number
from nexus_kernel.errors import WorkerError


logger = logging.getLogger(__name__)

WorkerRunner: TypeAlias = Callable[[asyncio.Event], Awaitable[None]]


class WorkerState(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    name: str
    runner: WorkerRunner
    required: bool = True
    shutdown_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", self.name) is None:
            raise ValueError("worker name must be a stable lowercase identifier")
        if not callable(self.runner):
            raise ValueError("worker runner must be callable")
        require_positive_finite_number(
            self.shutdown_timeout_seconds, "worker shutdown timeout"
        )


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    name: str
    required: bool
    state: WorkerState
    error: str | None = None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "name": self.name,
            "required": self.required,
            "state": self.state.value,
            "error": self.error,
        }


WorkerStatusListener: TypeAlias = Callable[[WorkerStatus], None]


@dataclass(slots=True)
class _WorkerEntry:
    spec: WorkerSpec
    state: WorkerState = WorkerState.REGISTERED
    error: str | None = None
    task: asyncio.Task[None] | None = None
    stop_cancel_requested: bool = False
    stop_timed_out: bool = False


class WorkerSupervisor:
    def __init__(self, *, status_listener: WorkerStatusListener | None = None) -> None:
        self._entries: dict[str, _WorkerEntry] = {}
        self._status_listener = status_listener
        self._stop_event = asyncio.Event()
        self._required_failure = asyncio.Event()
        self._started = False
        self._stopped = False
        self._stop_lock = asyncio.Lock()

    @property
    def shutdown_complete(self) -> bool:
        """Return whether every registered Worker task is terminal."""

        return self._stopped

    def register(self, spec: WorkerSpec) -> None:
        if self._started:
            raise WorkerError("Workers cannot be registered after startup")
        if spec.name in self._entries:
            raise WorkerError(f"Worker is already registered: {spec.name}")
        entry = _WorkerEntry(spec=spec)
        self._entries[spec.name] = entry
        self._transition(entry, WorkerState.REGISTERED)

    async def start_all(self) -> None:
        if self._started:
            return
        if self._stopped:
            raise WorkerError("A stopped WorkerSupervisor cannot be restarted")
        self._started = True
        for entry in self._entries.values():
            self._transition(entry, WorkerState.RUNNING)
            entry.task = asyncio.create_task(
                self._run(entry), name=f"nexus_kernel_worker_{entry.spec.name}"
            )
        await asyncio.sleep(0)
        errors = self._required_failure_errors()
        if errors:
            raise WorkerError("Required worker failed during startup", errors=errors)

    async def _run(self, entry: _WorkerEntry) -> None:
        try:
            await entry.spec.runner(self._stop_event)
        except asyncio.CancelledError:
            if not entry.stop_timed_out:
                self._transition(entry, WorkerState.STOPPED)
            raise
        except Exception:
            if not entry.stop_timed_out:
                self._fail(entry, "worker_failed")
        else:
            if entry.stop_timed_out:
                return
            if self._stop_event.is_set():
                self._transition(entry, WorkerState.STOPPED)
            else:
                self._fail(entry, "worker_exited")

    def _fail(self, entry: _WorkerEntry, error: str) -> None:
        self._transition(entry, WorkerState.FAILED, error)
        log = logger.error if entry.spec.required else logger.warning
        log("%s name=%s required=%s", error, entry.spec.name, entry.spec.required)
        if entry.spec.required:
            self._required_failure.set()

    def _transition(
        self,
        entry: _WorkerEntry,
        state: WorkerState,
        error: str | None = None,
    ) -> None:
        entry.state = state
        entry.error = error
        listener = self._status_listener
        if listener is None:
            return
        try:
            listener(self._status(entry))
        except asyncio.CancelledError:
            logger.warning("worker_status_listener_failed name=%s", entry.spec.name)
        except Exception:
            logger.warning("worker_status_listener_failed name=%s", entry.spec.name)

    @staticmethod
    def _status(entry: _WorkerEntry) -> WorkerStatus:
        return WorkerStatus(
            name=entry.spec.name,
            required=entry.spec.required,
            state=entry.state,
            error=entry.error,
        )

    def _required_failure_errors(self) -> tuple[str, ...]:
        return tuple(
            f"{entry.error}:{entry.spec.name}"
            for entry in self._entries.values()
            if entry.spec.required
            and entry.state is WorkerState.FAILED
            and entry.error is not None
        )

    async def wait_required_failure(self) -> None:
        await self._required_failure.wait()
        raise WorkerError(
            "Required worker failed", errors=self._required_failure_errors()
        )

    async def stop_all(self) -> list[str]:
        async with self._stop_lock:
            if self._stopped:
                return []
            self._stop_event.set()
            errors: list[str] = []
            pending = False
            for entry in reversed(tuple(self._entries.values())):
                error, entry_pending = await self._stop_entry(entry)
                pending = pending or entry_pending
                if error is not None:
                    errors.append(error)
            self._stopped = not pending
            return errors

    async def _stop_entry(self, entry: _WorkerEntry) -> tuple[str | None, bool]:
        task = entry.task
        if task is None:
            self._transition(entry, WorkerState.STOPPED)
            return None, False
        if not task.done():
            done, _ = await asyncio.wait(
                {task}, timeout=entry.spec.shutdown_timeout_seconds
            )
            if not done:
                if not entry.stop_timed_out:
                    entry.stop_timed_out = True
                    self._transition(entry, WorkerState.FAILED, "worker_stop_timeout")
                    if entry.spec.required:
                        self._required_failure.set()
                    log = logger.error if entry.spec.required else logger.warning
                    log(
                        "worker_stop_timeout name=%s required=%s",
                        entry.spec.name,
                        entry.spec.required,
                    )
                if not entry.stop_cancel_requested:
                    entry.stop_cancel_requested = True
                    task.cancel()
                await asyncio.sleep(0)

        pending = not task.done()
        if entry.state is WorkerState.FAILED and (entry.spec.required or pending):
            error = entry.error or "worker_failed"
            return f"{error}:{entry.spec.name}", pending
        return None, pending

    def statuses(self) -> list[WorkerStatus]:
        return [self._status(entry) for entry in self._entries.values()]

    def health_status(self) -> dict[str, object]:
        statuses = self.statuses()
        if any(
            status.required and status.state is WorkerState.FAILED
            for status in statuses
        ):
            health = "unhealthy"
        elif any(status.state is WorkerState.FAILED for status in statuses):
            health = "degraded"
        else:
            health = "healthy"
        return {
            "status": health,
            "workers": [status.to_dict() for status in statuses],
        }
