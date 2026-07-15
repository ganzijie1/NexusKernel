"""Lifecycle supervision for cooperative background workers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeAlias

from nexuskernel.errors import WorkerError


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
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("worker shutdown timeout must be positive")


@dataclass(slots=True)
class _WorkerEntry:
    spec: WorkerSpec
    state: WorkerState = WorkerState.REGISTERED
    error: str | None = None
    task: asyncio.Task[None] | None = None


class WorkerSupervisor:
    def __init__(self) -> None:
        self._entries: dict[str, _WorkerEntry] = {}
        self._stop_event = asyncio.Event()
        self._started = False
        self._stopped = False

    def register(self, spec: WorkerSpec) -> None:
        if self._started:
            raise WorkerError("Workers cannot be registered after startup")
        if spec.name in self._entries:
            raise WorkerError(f"Worker is already registered: {spec.name}")
        self._entries[spec.name] = _WorkerEntry(spec=spec)

    async def start_all(self) -> None:
        if self._started:
            return
        if self._stopped:
            raise WorkerError("A stopped WorkerSupervisor cannot be restarted")
        self._started = True
        for entry in self._entries.values():
            entry.state = WorkerState.RUNNING
            entry.task = asyncio.create_task(
                self._run(entry), name=f"nexuskernel-worker-{entry.spec.name}"
            )
        await asyncio.sleep(0)

    async def _run(self, entry: _WorkerEntry) -> None:
        try:
            await entry.spec.runner(self._stop_event)
        except asyncio.CancelledError:
            entry.state = WorkerState.STOPPED
            entry.error = None
            raise
        except Exception:
            entry.state = WorkerState.FAILED
            entry.error = "worker_failed"
        else:
            if self._stop_event.is_set():
                entry.state = WorkerState.STOPPED
                entry.error = None
            else:
                entry.state = WorkerState.FAILED
                entry.error = "worker_exited"

    async def stop_all(self) -> list[str]:
        if self._stopped:
            return []
        self._stop_event.set()
        errors: list[str] = []
        for entry in reversed(tuple(self._entries.values())):
            task = entry.task
            if task is None:
                entry.state = WorkerState.STOPPED
                continue
            if task.done():
                if entry.state is WorkerState.FAILED:
                    errors.append(f"{entry.error}:{entry.spec.name}")
                continue
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=entry.spec.shutdown_timeout_seconds,
                )
            except TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                entry.state = WorkerState.FAILED
                entry.error = "worker_stop_timeout"
                errors.append(f"worker_stop_timeout:{entry.spec.name}")
        self._stopped = True
        return errors

    def statuses(self) -> list[dict[str, Any]]:
        return [
            {
                "name": entry.spec.name,
                "required": entry.spec.required,
                "state": entry.state.value,
                "error": entry.error,
            }
            for entry in self._entries.values()
        ]
