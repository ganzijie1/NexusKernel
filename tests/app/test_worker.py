from __future__ import annotations

import asyncio
from typing import cast

import pytest

from app.bootstrap import ApplicationRuntime
from app.worker import run_worker
from nexus_kernel.errors import WorkerError
from nexus_kernel.runtime import WorkerSpec, WorkerSupervisor


class _DelayedFailureRuntime:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.workers = WorkerSupervisor()
        self.workers.register(
            WorkerSpec("delayed_failure", self._run_worker, required=True)
        )
        self.started = asyncio.Event()
        self.stop_calls = 0

    async def _run_worker(self, stop_event: asyncio.Event) -> None:
        await self.release.wait()
        raise RuntimeError("private worker failure")

    async def start(self) -> None:
        await self.workers.start_all()
        self.started.set()

    async def stop(self) -> None:
        self.stop_calls += 1
        await self.workers.stop_all()


@pytest.mark.asyncio
async def test_run_worker_exits_and_cleans_up_after_delayed_required_failure() -> None:
    runtime = _DelayedFailureRuntime()
    task = asyncio.create_task(
        run_worker(cast(ApplicationRuntime, runtime), asyncio.Event())
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=0.2)

    runtime.release.set()
    with pytest.raises(WorkerError) as captured:
        await asyncio.wait_for(task, timeout=0.2)

    assert captured.value.errors == ("worker_failed:delayed_failure",)
    assert runtime.stop_calls == 1


@pytest.mark.asyncio
async def test_run_worker_does_not_load_api_only_agent_registry(monkeypatch) -> None:
    runtime = _DelayedFailureRuntime()
    captured: list[dict[str, object]] = []

    def fake_build_runtime(**kwargs):
        captured.append(kwargs)
        return runtime

    monkeypatch.setattr("app.worker.build_runtime", fake_build_runtime)
    stop_event = asyncio.Event()
    stop_event.set()

    await run_worker(None, stop_event)

    assert captured == [{}]
    assert runtime.stop_calls == 1
