from __future__ import annotations

import asyncio
from typing import Any, Protocol

import pytest

from nexuskernel.errors import (
    KernelStartupError,
    ModuleAccessError,
    ModuleDependencyError,
    ModuleLookupError,
)
from nexuskernel.kernel import Kernel, LoadedModule, Module, ModuleState


def _loaded(module: Module, index: int, *, required: bool = True) -> LoadedModule:
    return LoadedModule(module=module, required=required, original_index=index)


class RecordingModule(Module):
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        dependencies: tuple[str, ...] = (),
        optional_dependencies: tuple[str, ...] = (),
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        block_start: bool = False,
    ) -> None:
        super().__init__()
        self.name = name
        self.dependencies = dependencies
        self.optional_dependencies = optional_dependencies
        self.events = events
        self.start_error = start_error
        self.stop_error = stop_error
        self.block_start = block_start

    def on_register(self, kernel: Any) -> None:
        self.events.append(f"register:{self.name}")

    async def on_start(self, kernel: Any) -> None:
        self.events.append(f"start:{self.name}")
        if self.block_start:
            await asyncio.sleep(60)
        if self.start_error is not None:
            raise self.start_error

    async def on_stop(self, kernel: Any) -> None:
        self.events.append(f"stop:{self.name}")
        if self.stop_error is not None:
            raise self.stop_error


def test_kernel_rejects_duplicate_module_names() -> None:
    events: list[str] = []

    with pytest.raises(ModuleDependencyError, match="duplicate"):
        Kernel(
            [
                _loaded(RecordingModule("same", events), 0),
                _loaded(RecordingModule("same", events), 1),
            ]
        )


def test_kernel_rejects_missing_and_cyclic_dependencies() -> None:
    events: list[str] = []
    missing = RecordingModule("missing-user", events, dependencies=("absent",))

    with pytest.raises(ModuleDependencyError, match="absent"):
        Kernel([_loaded(missing, 0)])

    first = RecordingModule("first", events, dependencies=("second",))
    second = RecordingModule("second", events, dependencies=("first",))
    with pytest.raises(ModuleDependencyError, match="cycle"):
        Kernel([_loaded(first, 0), _loaded(second, 1)])


@pytest.mark.asyncio
async def test_kernel_uses_stable_topology_and_reverse_stop_order() -> None:
    events: list[str] = []
    feature = RecordingModule("feature", events, dependencies=("base",))
    independent = RecordingModule("independent", events)
    base = RecordingModule("base", events)
    kernel = Kernel(
        [
            _loaded(feature, 0),
            _loaded(independent, 1),
            _loaded(base, 2),
        ]
    )

    assert kernel.module_order == ("independent", "base", "feature")

    await kernel.start()
    errors = await kernel.stop()

    assert errors == []
    assert events == [
        "register:independent",
        "register:base",
        "register:feature",
        "start:independent",
        "start:base",
        "start:feature",
        "stop:feature",
        "stop:base",
        "stop:independent",
    ]
    assert all(item["state"] == "stopped" for item in kernel.module_statuses())


@pytest.mark.asyncio
async def test_required_start_failure_rolls_back_without_leaking_error() -> None:
    events: list[str] = []
    first = RecordingModule("first", events)
    broken = RecordingModule(
        "broken", events, start_error=RuntimeError("Authorization: Bearer secret")
    )
    kernel = Kernel([_loaded(first, 0), _loaded(broken, 1)])

    with pytest.raises(KernelStartupError) as captured:
        await kernel.start()

    assert "secret" not in str(captured.value)
    assert events[-1] == "stop:first"
    assert first.state is ModuleState.STOPPED
    assert broken.state is ModuleState.FAILED


@pytest.mark.asyncio
async def test_optional_module_failure_degrades_and_does_not_abort_startup() -> None:
    events: list[str] = []
    optional = RecordingModule(
        "optional", events, start_error=RuntimeError("password=secret")
    )
    ready = RecordingModule("ready", events)
    kernel = Kernel([_loaded(optional, 0, required=False), _loaded(ready, 1)])

    await kernel.start()

    assert optional.state is ModuleState.DEGRADED
    assert ready.state is ModuleState.READY
    assert kernel.health_status()["status"] == "degraded"
    assert "secret" not in repr(kernel.module_statuses())
    await kernel.stop()


@pytest.mark.asyncio
async def test_start_cancellation_propagates_after_rollback() -> None:
    events: list[str] = []
    first = RecordingModule("first", events)
    blocking = RecordingModule("blocking", events, block_start=True)
    kernel = Kernel([_loaded(first, 0), _loaded(blocking, 1)])

    task = asyncio.create_task(kernel.start())
    while "start:blocking" not in events:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events[-1] == "stop:first"
    assert first.state is ModuleState.STOPPED
    assert blocking.state is ModuleState.FAILED


class PublicApi(Protocol):
    def exposed(self) -> str: ...


class ContractModule(Module):
    name = "contract"
    api_contract = PublicApi

    def exposed(self) -> str:
        return "ok"

    def internal_secret(self) -> str:
        return "must-not-be-callable"


@pytest.mark.asyncio
async def test_module_proxy_enforces_protocol_api_whitelist() -> None:
    kernel = Kernel([_loaded(ContractModule(), 0)])
    await kernel.start()

    proxy = kernel.get_module("contract")

    assert proxy.exposed() == "ok"
    with pytest.raises(ModuleAccessError):
        proxy.internal_secret()
    with pytest.raises(ModuleAccessError):
        proxy.state
    with pytest.raises(ModuleAccessError):
        proxy._ModuleApiProxy__target
    with pytest.raises(ModuleLookupError):
        kernel.get_module("missing")
    await kernel.stop()


@pytest.mark.asyncio
async def test_stop_aggregates_errors_and_continues_in_reverse_order() -> None:
    events: list[str] = []
    first = RecordingModule(
        "first", events, stop_error=RuntimeError("credential-url=secret")
    )
    second = RecordingModule("second", events)
    kernel = Kernel([_loaded(first, 0), _loaded(second, 1)])
    await kernel.start()

    errors = await kernel.stop()
    second_errors = await kernel.stop()

    assert events[-2:] == ["stop:second", "stop:first"]
    assert errors == ["module_stop_failed:first"]
    assert "secret" not in repr(errors)
    assert second_errors == []
