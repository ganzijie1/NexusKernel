from __future__ import annotations

import asyncio
from typing import Protocol

import pytest

from nexus_kernel.errors import (
    KernelStartupError,
    ModuleAccessError,
    ModuleDependencyError,
    ModuleLookupError,
    WorkerError,
)
from nexus_kernel.kernel import (
    HttpEndpointSpec,
    Kernel,
    LoadedModule,
    Module,
    ModuleContext,
    ModuleState,
)
from nexus_kernel.runtime import WorkerSpec


def _loaded(module: Module, index: int, *, required: bool = True) -> LoadedModule:
    return LoadedModule(module=module, required=required, original_index=index)


async def _idle_worker(stop_event: asyncio.Event) -> None:
    await stop_event.wait()


class WorkerModule(Module):
    def __init__(self, name: str, specs: tuple[WorkerSpec, ...]) -> None:
        super().__init__()
        self.name = name
        self._specs = specs

    def get_worker_specs(self) -> list[WorkerSpec]:
        return list(self._specs)


@pytest.mark.asyncio
async def test_kernel_aggregates_operational_worker_specs_in_stable_order() -> None:
    second = WorkerModule("second", (WorkerSpec("worker_b", _idle_worker),))
    first = WorkerModule("first", (WorkerSpec("worker_a", _idle_worker),))
    kernel = Kernel([_loaded(second, 1), _loaded(first, 0)])

    assert kernel.worker_specs() == []
    await kernel.start()
    assert [spec.name for spec in kernel.worker_specs()] == ["worker_a", "worker_b"]
    await kernel.stop()


def test_kernel_rejects_duplicate_worker_names_across_modules() -> None:
    first = WorkerModule("first", (WorkerSpec("duplicate", _idle_worker),))
    second = WorkerModule("second", (WorkerSpec("duplicate", _idle_worker),))
    with pytest.raises(WorkerError, match="duplicated"):
        Kernel([_loaded(first, 0), _loaded(second, 1)])


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

    def on_register(self, context: ModuleContext) -> None:
        self.events.append(f"register:{self.name}")

    async def on_start(self, context: ModuleContext) -> None:
        self.events.append(f"start:{self.name}")
        if self.block_start:
            await asyncio.sleep(60)
        if self.start_error is not None:
            raise self.start_error

    async def on_stop(self, context: ModuleContext) -> None:
        self.events.append(f"stop:{self.name}")
        if self.stop_error is not None:
            raise self.stop_error


class ConcurrentStartModule(Module):
    name = "concurrent_start"

    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.register_count = 0
        self.start_count = 0
        self.stop_count = 0

    def on_register(self, context: ModuleContext) -> None:
        self.register_count += 1

    async def on_start(self, context: ModuleContext) -> None:
        self.start_count += 1
        self.start_entered.set()
        await self.release_start.wait()

    async def on_stop(self, context: ModuleContext) -> None:
        self.stop_count += 1


@pytest.mark.asyncio
async def test_concurrent_kernel_start_runs_lifecycle_once() -> None:
    module = ConcurrentStartModule()
    kernel = Kernel([_loaded(module, 0)])
    first = asyncio.create_task(kernel.start())
    await module.start_entered.wait()
    second = asyncio.create_task(kernel.start())
    await asyncio.sleep(0)

    module.release_start.set()
    await asyncio.gather(first, second)
    await kernel.stop()

    assert (module.register_count, module.start_count, module.stop_count) == (1, 1, 1)


class CallableApi(Protocol):
    def execute(self) -> str: ...


class NonCallableApiModule(Module):
    name = "non_callable_api"
    api_contract = CallableApi
    execute = "not-callable"


def test_kernel_rejects_non_callable_protocol_implementation() -> None:
    with pytest.raises(ModuleDependencyError, match="API implementation"):
        Kernel([_loaded(NonCallableApiModule(), 0)])


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

    assert optional.state is ModuleState.FAILED
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
async def test_cached_proxy_callable_rechecks_module_state_on_each_call() -> None:
    kernel = Kernel([_loaded(ContractModule(), 0)])
    await kernel.start()
    exposed = kernel.get_module("contract").exposed

    assert exposed() == "ok"
    await kernel.stop()

    with pytest.raises(ModuleDependencyError, match="unavailable"):
        exposed()


class RoutedModule(ContractModule):
    http_endpoints = (
        HttpEndpointSpec(path="/contract", methods=("GET",), target_api="exposed"),
    )


@pytest.mark.asyncio
async def test_degraded_module_api_and_routes_remain_callable() -> None:
    module = RoutedModule()
    module.optional_provider_names = ("missing",)
    kernel = Kernel([_loaded(module, 0)])

    await kernel.start()

    assert module.state is ModuleState.DEGRADED
    assert kernel.get_module("contract").exposed() == "ok"
    assert kernel.route_bindings()[0].handler() == "ok"
    await kernel.stop()


@pytest.mark.asyncio
async def test_missing_optional_module_degrades_consumer_and_accessor_returns_none() -> (
    None
):
    observed: list[object | None] = []

    class Consumer(Module):
        name = "consumer"
        optional_dependencies = ("missing",)

        async def on_start(self, context: ModuleContext) -> None:
            observed.append(context.optional_module("missing"))

    module = Consumer()
    kernel = Kernel([_loaded(module, 0)])

    await kernel.start()

    assert observed == [None]
    assert module.state is ModuleState.DEGRADED
    await kernel.stop()


@pytest.mark.asyncio
async def test_degraded_required_dependency_degrades_consumer_without_blocking() -> (
    None
):
    events: list[str] = []
    dependency = RecordingModule("dependency", events)
    dependency.optional_provider_names = ("missing",)
    consumer = RecordingModule("consumer", events, dependencies=("dependency",))
    kernel = Kernel([_loaded(dependency, 0), _loaded(consumer, 1)])

    await kernel.start()

    assert dependency.state is ModuleState.DEGRADED
    assert consumer.state is ModuleState.DEGRADED
    assert "start:consumer" in events
    await kernel.stop()


@pytest.mark.asyncio
async def test_registration_failure_cleans_all_registered_modules_once_in_reverse() -> (
    None
):
    events: list[str] = []

    class BrokenRegistration(RecordingModule):
        def on_register(self, context: ModuleContext) -> None:
            super().on_register(context)
            raise RuntimeError("private registration detail")

    first = RecordingModule("first", events)
    broken = BrokenRegistration("broken", events)
    kernel = Kernel([_loaded(first, 0), _loaded(broken, 1)])

    with pytest.raises(KernelStartupError):
        await kernel.start()
    assert events == [
        "register:first",
        "register:broken",
        "stop:broken",
        "stop:first",
    ]
    assert broken.state is ModuleState.FAILED
    assert await kernel.stop() == []


@pytest.mark.asyncio
async def test_optional_registration_failure_is_failed_and_does_not_double_clean() -> (
    None
):
    events: list[str] = []

    class BrokenRegistration(RecordingModule):
        def on_register(self, context: ModuleContext) -> None:
            super().on_register(context)
            raise RuntimeError("private registration detail")

    broken = BrokenRegistration("broken", events)
    ready = RecordingModule("ready", events)
    kernel = Kernel(
        [_loaded(broken, 0, required=False), _loaded(ready, 1, required=True)]
    )

    await kernel.start()
    await kernel.stop()

    assert broken.state is ModuleState.FAILED
    assert events.count("stop:broken") == 1
    assert ready.state is ModuleState.STOPPED


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
