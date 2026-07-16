from __future__ import annotations

import asyncio

import pytest

from nexus_kernel.errors import WorkerError
from nexus_kernel.runtime import (
    WorkerSpec,
    WorkerState,
    WorkerStatus,
    WorkerSupervisor,
)


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
    assert supervisor.statuses()[0].state is WorkerState.STOPPED


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
    assert status == WorkerStatus(
        name="broken",
        required=False,
        state=WorkerState.FAILED,
        error="worker_failed",
    )
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


@pytest.mark.asyncio
async def test_required_worker_failure_aborts_supervisor_start() -> None:
    async def failing_runner(stop_event: asyncio.Event) -> None:
        raise RuntimeError("password=must-not-leak")

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("required_failure", failing_runner, required=True))

    with pytest.raises(WorkerError) as captured:
        await supervisor.start_all()

    assert "must-not-leak" not in str(captured.value)
    assert supervisor.statuses()[0].error == "worker_failed"


@pytest.mark.asyncio
async def test_wait_required_failure_reports_delayed_failure_safely() -> None:
    release = asyncio.Event()

    async def delayed_failure(stop_event: asyncio.Event) -> None:
        await release.wait()
        raise RuntimeError("password=must-not-leak")

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("delayed_failure", delayed_failure, required=True))
    await supervisor.start_all()

    release.set()
    with pytest.raises(WorkerError) as captured:
        await asyncio.wait_for(supervisor.wait_required_failure(), timeout=0.2)

    assert captured.value.errors == ("worker_failed:delayed_failure",)
    assert "must-not-leak" not in str(captured.value)
    await supervisor.stop_all()


@pytest.mark.asyncio
async def test_worker_failure_during_stop_is_aggregated() -> None:
    async def failing_on_stop(stop_event: asyncio.Event) -> None:
        await stop_event.wait()
        raise RuntimeError("private worker failure")

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("stop_failure", failing_on_stop))
    await supervisor.start_all()

    errors = await supervisor.stop_all()

    assert errors == ["worker_failed:stop_failure"]


@pytest.mark.asyncio
async def test_worker_ignoring_first_cancel_cannot_hang_shutdown() -> None:
    cancel_count = 0

    async def ignores_first_cancel(stop_event: asyncio.Event) -> None:
        nonlocal cancel_count
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancel_count += 1
                if cancel_count >= 2:
                    raise

    supervisor = WorkerSupervisor()
    supervisor.register(
        WorkerSpec(
            "cancel_resistant", ignores_first_cancel, shutdown_timeout_seconds=0.01
        )
    )
    await supervisor.start_all()

    started_at = asyncio.get_running_loop().time()
    errors = await asyncio.wait_for(supervisor.stop_all(), timeout=0.2)
    elapsed = asyncio.get_running_loop().time() - started_at

    assert errors == ["worker_stop_timeout:cancel_resistant"]
    assert cancel_count >= 2
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_status_listener_receives_immutable_transitions_and_is_isolated() -> None:
    observed: list[WorkerStatus] = []

    def listener(status: WorkerStatus) -> None:
        observed.append(status)
        if status.state is WorkerState.RUNNING:
            raise RuntimeError("listener failure must be isolated")

    async def runner(stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    supervisor = WorkerSupervisor(status_listener=listener)
    supervisor.register(WorkerSpec("observed", runner, required=False))

    await supervisor.start_all()
    await supervisor.stop_all()

    assert [status.state for status in observed] == [
        WorkerState.REGISTERED,
        WorkerState.RUNNING,
        WorkerState.STOPPED,
    ]
    with pytest.raises(AttributeError):
        observed[-1].error = "changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_optional_worker_failure_degrades_health_without_required_signal() -> (
    None
):
    async def broken(stop_event: asyncio.Event) -> None:
        raise RuntimeError("private worker detail")

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("optional", broken, required=False))
    await supervisor.start_all()
    await asyncio.sleep(0)

    assert supervisor.health_status() == {
        "status": "degraded",
        "workers": [
            {
                "name": "optional",
                "required": False,
                "state": "failed",
                "error": "worker_failed",
            }
        ],
    }
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(supervisor.wait_required_failure(), timeout=0.01)
    assert await supervisor.stop_all() == []


@pytest.mark.asyncio
async def test_required_worker_failure_is_unhealthy() -> None:
    async def broken(stop_event: asyncio.Event) -> None:
        raise RuntimeError("private worker detail")

    supervisor = WorkerSupervisor()
    supervisor.register(WorkerSpec("required", broken))
    with pytest.raises(WorkerError):
        await supervisor.start_all()

    assert supervisor.health_status()["status"] == "unhealthy"
