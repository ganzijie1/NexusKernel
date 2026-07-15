from __future__ import annotations

import asyncio

import pytest

from nexuskernel.errors import WorkerError
from nexuskernel.runtime import WorkerSpec, WorkerSupervisor


@pytest.mark.asyncio
async def test_worker_supervisor_starts_and_gracefully_stops_workers() -> None:
    events: list[str] = []

    async def runner(stop_event: asyncio.Event) -> None:
        events.append("started")
        await stop_event.wait()
        events.append("stopped")

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("example", runner))

    await supervisor.start_all()
    errors = await supervisor.stop_all()
    second_errors = await supervisor.stop_all()

    assert events == ["started", "stopped"]
    assert errors == []
    assert second_errors == []
    assert supervisor.statuses()[0]["state"] == "stopped"


@pytest.mark.asyncio
async def test_worker_timeout_cancels_task_and_returns_safe_error() -> None:
    cancelled = asyncio.Event()

    async def ignores_stop(stop_event: asyncio.Event) -> None:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("slow", ignores_stop, shutdown_timeout_seconds=0.01))
    await supervisor.start_all()

    errors = await supervisor.stop_all()

    assert cancelled.is_set()
    assert errors == ["worker_stop_timeout:slow"]


@pytest.mark.asyncio
async def test_worker_failure_is_redacted_in_status() -> None:
    async def broken(stop_event: asyncio.Event) -> None:
        raise RuntimeError("password=must-not-leak")

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("broken", broken, required=False))
    await supervisor.start_all()
    await asyncio.sleep(0)

    status = supervisor.statuses()[0]
    assert status == {
        "name": "broken",
        "required": False,
        "state": "failed",
        "error": "worker_failed",
    }
    assert "must-not-leak" not in repr(status)
    await supervisor.stop_all()


def test_worker_supervisor_rejects_duplicate_names() -> None:
    async def runner(stop_event: asyncio.Event) -> None:
        return None

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("same", runner))
    with pytest.raises(WorkerError, match="already registered"):
        supervisor.register(WorkerSpec("same", runner))


@pytest.mark.parametrize("name", ["", "UPPER", "bad/name", " leading"])
def test_worker_spec_requires_stable_name(name: str) -> None:
    async def runner(stop_event: asyncio.Event) -> None:
        return None

    with pytest.raises(ValueError):
        WorkerSpec(name, runner)
