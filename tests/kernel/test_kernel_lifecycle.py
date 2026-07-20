from __future__ import annotations

import asyncio
import itertools
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
from nexus_kernel.providers import ProviderRegistry, ProviderSpec
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
    def __new__(
        cls,
        name: str | None = None,
        events: list[str] | None = None,
        *,
        dependencies: tuple[str, ...] = (),
        optional_dependencies: tuple[str, ...] = (),
        required_provider_names: tuple[str, ...] = (),
        optional_provider_names: tuple[str, ...] = (),
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        block_start: bool = False,
    ) -> RecordingModule:
        if name is None:
            return object.__new__(cls)
        if events is None:
            raise TypeError("recording events are required")
        declared_type = type(
            f"Declared{name.title().replace('-', '')}{cls.__name__}",
            (cls,),
            {
                "name": name,
                "dependencies": dependencies,
                "optional_dependencies": optional_dependencies,
                "required_provider_names": required_provider_names,
                "optional_provider_names": optional_provider_names,
            },
        )
        return object.__new__(declared_type)

    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        dependencies: tuple[str, ...] = (),
        optional_dependencies: tuple[str, ...] = (),
        required_provider_names: tuple[str, ...] = (),
        optional_provider_names: tuple[str, ...] = (),
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        block_start: bool = False,
    ) -> None:
        super().__init__()
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
    with pytest.raises(ModuleDependencyError, match=r"first -> second -> first"):
        Kernel([_loaded(first, 0), _loaded(second, 1)])


def test_kernel_revalidates_loader_dependency_manifest() -> None:
    class Dependency(Module):
        name = "dependency"

    class Consumer(Module):
        name = "consumer"
        dependencies = ("dependency",)

    dependency = LoadedModule(
        module=Dependency(),
        required=True,
        original_index=1,
        manifest_dependencies=(),
        manifest_optional_dependencies=(),
    )
    consumer_module = Consumer()
    consumer = LoadedModule(
        module=consumer_module,
        required=True,
        original_index=0,
        manifest_dependencies=("dependency",),
        manifest_optional_dependencies=(),
    )
    consumer_module.dependencies = ()

    with pytest.raises(ModuleDependencyError, match="manifest does not match"):
        Kernel([consumer, dependency])


def test_kernel_rejects_class_instance_drift_without_manifest() -> None:
    class ProgrammaticModule(Module):
        name = "programmatic"
        required_provider_names = ("database",)

    module = ProgrammaticModule()
    module.required_provider_names = ()

    with pytest.raises(ModuleDependencyError, match="class does not match instance"):
        Kernel([_loaded(module, 0)])


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("dependencies", ["dependency"]),
        ("dependencies", ("",)),
        ("dependencies", ("dependency", "dependency")),
        ("dependencies", ("invalid",)),
        ("optional_dependencies", ("invalid",)),
        ("required_provider_names", ["database"]),
        ("required_provider_names", ("",)),
        ("required_provider_names", ("Database",)),
        ("required_provider_names", ("database-name",)),
        ("required_provider_names", ("database", "database")),
    ],
)
def test_kernel_rejects_invalid_programmatic_capability_declarations(
    attribute: str,
    value: object,
) -> None:
    declarations: dict[str, object] = {"name": "invalid"}
    declarations[attribute] = value
    if attribute == "optional_dependencies" and value == ("invalid",):
        declarations["dependencies"] = ("invalid",)
    invalid_type = type("InvalidDeclarations", (Module,), declarations)

    with pytest.raises(ModuleDependencyError, match="declaration"):
        Kernel([_loaded(invalid_type(), 0)])


def test_kernel_rejects_required_optional_provider_overlap() -> None:
    class InvalidProviders(Module):
        name = "invalid_providers"
        required_provider_names = ("database",)
        optional_provider_names = ("database",)

    with pytest.raises(ModuleDependencyError, match="declarations are invalid"):
        Kernel([_loaded(InvalidProviders(), 0)])


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

    assert kernel.module_order == ("base", "feature", "independent")

    await kernel.start()
    errors = await kernel.stop()

    assert errors == []
    assert events == [
        "register:base",
        "register:feature",
        "register:independent",
        "start:base",
        "start:feature",
        "start:independent",
        "stop:independent",
        "stop:feature",
        "stop:base",
    ]
    assert all(item["state"] == "stopped" for item in kernel.module_statuses())


@pytest.mark.asyncio
async def test_kernel_lifecycle_order_is_stable_for_chain_and_diamond_permutations() -> (
    None
):
    graphs = (
        {
            "alpha": (),
            "beta": ("alpha",),
            "gamma": ("beta",),
        },
        {
            "alpha": (),
            "beta": ("alpha",),
            "gamma": ("alpha",),
            "omega": ("beta", "gamma"),
        },
    )
    for graph in graphs:
        expected_order = tuple(graph)
        for permutation in itertools.permutations(graph):
            events: list[str] = []
            modules = {
                name: RecordingModule(name, events, dependencies=graph[name])
                for name in graph
            }
            kernel = Kernel(
                [
                    _loaded(modules[name], index)
                    for index, name in enumerate(permutation)
                ]
            )

            assert kernel.module_order == expected_order
            await kernel.start()
            assert await kernel.stop() == []
            assert events == [
                *(f"register:{name}" for name in expected_order),
                *(f"start:{name}" for name in expected_order),
                *(f"stop:{name}" for name in reversed(expected_order)),
            ]


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


@pytest.mark.asyncio
async def test_repeated_start_cancellation_waits_for_module_cleanup() -> None:
    events: list[str] = []
    start_entered = asyncio.Event()
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class BlockingCleanupModule(RecordingModule):
        name = "blocking"

        def __init__(self) -> None:
            Module.__init__(self)
            self.events = events
            self.start_error = None
            self.stop_error = None
            self.block_start = False
            self.cleaned = False

        async def on_start(self, context: ModuleContext) -> None:
            events.append("start:blocking")
            start_entered.set()
            await asyncio.Event().wait()

        async def on_stop(self, context: ModuleContext) -> None:
            events.append("stop:blocking")
            stop_entered.set()
            await release_stop.wait()
            self.cleaned = True

    first = RecordingModule("first", events)
    blocking = BlockingCleanupModule()
    kernel = Kernel([_loaded(first, 0), _loaded(blocking, 1)])
    starting = asyncio.create_task(kernel.start())
    await start_entered.wait()

    starting.cancel()
    await stop_entered.wait()
    starting.cancel()
    await asyncio.sleep(0)

    assert not starting.done()
    assert blocking.cleaned is False

    release_stop.set()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert blocking.cleaned is True
    assert first.state is ModuleState.STOPPED
    assert await kernel.stop() == []


@pytest.mark.asyncio
async def test_stop_hook_self_cancellation_is_stable_and_does_not_block_reverse_cleanup() -> (
    None
):
    events: list[str] = []

    class SelfCancellingStopModule(RecordingModule):
        async def on_stop(self, context: ModuleContext) -> None:
            events.append(f"stop:{self.name}")
            raise asyncio.CancelledError

    first = RecordingModule("first", events)
    second = SelfCancellingStopModule("second", events)
    kernel = Kernel([_loaded(first, 0), _loaded(second, 1)])
    await kernel.start()

    errors = await kernel.stop()

    assert errors == ["module_stop_failed:second"]
    assert events[-2:] == ["stop:second", "stop:first"]
    assert second.state is ModuleState.FAILED
    assert kernel.module_statuses()[1]["error"] == "module_stop_failed"
    assert await kernel.stop() == []


@pytest.mark.asyncio
async def test_stop_hook_self_cancellation_does_not_replace_start_failure() -> None:
    events: list[str] = []

    class FailedStartAndStopModule(RecordingModule):
        async def on_start(self, context: ModuleContext) -> None:
            events.append(f"start:{self.name}")
            raise RuntimeError("unavailable")

        async def on_stop(self, context: ModuleContext) -> None:
            events.append(f"stop:{self.name}")
            raise asyncio.CancelledError

    module = FailedStartAndStopModule("failed", events)
    kernel = Kernel([_loaded(module, 0)])

    with pytest.raises(KernelStartupError):
        await kernel.start()

    assert events == ["register:failed", "start:failed", "stop:failed"]
    assert kernel.module_statuses()[0]["error"] == "module_start_failed"
    assert await kernel.stop() == []


@pytest.mark.asyncio
async def test_repeated_stop_cancellation_converges_before_propagating() -> None:
    events: list[str] = []
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class BlockingStopModule(RecordingModule):
        async def on_stop(self, context: ModuleContext) -> None:
            events.append(f"stop:{self.name}")
            stop_entered.set()
            await release_stop.wait()
            events.append(f"stopped:{self.name}")

    module = BlockingStopModule("blocking", events)
    kernel = Kernel([_loaded(module, 0)])
    await kernel.start()
    stopping = asyncio.create_task(kernel.stop())
    await stop_entered.wait()

    stopping.cancel()
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)

    assert not stopping.done()
    release_stop.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert events[-2:] == ["stop:blocking", "stopped:blocking"]
    assert module.state is ModuleState.STOPPED
    assert await kernel.stop() == []


def test_kernel_rejects_invalid_module_cleanup_timeout() -> None:
    with pytest.raises(ValueError, match="positive finite"):
        Kernel([], module_cleanup_timeout_seconds=0)


@pytest.mark.asyncio
async def test_noncooperative_module_stop_preserves_dependencies_until_retry() -> None:
    events: list[str] = []
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class SharedProvider:
        async def start(self) -> None:
            events.append("provider:start")

        async def health_check(self) -> bool:
            return True

        async def close(self) -> None:
            events.append("provider:close")

    class ResistantModule(RecordingModule):
        async def on_stop(self, context: ModuleContext) -> None:
            events.append(f"stop:{self.name}")
            stop_entered.set()
            while not release_stop.is_set():
                try:
                    await release_stop.wait()
                except asyncio.CancelledError:
                    continue
            events.append(f"stopped:{self.name}")

    provider_registry = ProviderRegistry()
    provider_registry.register(ProviderSpec("shared", SharedProvider))
    dependency = RecordingModule("dependency", events)
    resistant = ResistantModule(
        "resistant",
        events,
        dependencies=("dependency",),
        required_provider_names=("shared",),
    )
    kernel = Kernel(
        [_loaded(dependency, 0), _loaded(resistant, 1)],
        providers=provider_registry,
        module_cleanup_timeout_seconds=0.01,
    )
    await kernel.start()

    first_errors = await kernel.stop()
    await stop_entered.wait()
    second_errors = await kernel.stop()

    assert first_errors == ["module_stop_timeout:resistant"]
    assert second_errors == ["module_stop_timeout:resistant"]
    assert kernel.shutdown_complete is False
    assert events.count("stop:resistant") == 1
    assert "stop:dependency" not in events
    assert "provider:close" not in events

    release_stop.set()
    await asyncio.sleep(0)
    assert await kernel.stop() == []
    assert kernel.shutdown_complete is True
    assert events[-3:] == [
        "stopped:resistant",
        "stop:dependency",
        "provider:close",
    ]


@pytest.mark.asyncio
async def test_startup_rollback_preserves_provider_while_module_cleanup_is_pending() -> (
    None
):
    events: list[str] = []
    release_stop = asyncio.Event()

    class SharedProvider:
        async def start(self) -> None:
            events.append("provider:start")

        async def health_check(self) -> bool:
            return True

        async def close(self) -> None:
            events.append("provider:close")

    class FailedModule(RecordingModule):
        async def on_start(self, context: ModuleContext) -> None:
            events.append(f"start:{self.name}")
            raise RuntimeError("private startup detail")

        async def on_stop(self, context: ModuleContext) -> None:
            events.append(f"stop:{self.name}")
            while not release_stop.is_set():
                try:
                    await release_stop.wait()
                except asyncio.CancelledError:
                    continue

    registry = ProviderRegistry()
    registry.register(ProviderSpec("shared", SharedProvider))
    module = FailedModule("failed", events, required_provider_names=("shared",))
    kernel = Kernel(
        [_loaded(module, 0)],
        providers=registry,
        module_cleanup_timeout_seconds=0.01,
    )

    with pytest.raises(KernelStartupError):
        await kernel.start()

    assert kernel.shutdown_complete is False
    assert "provider:close" not in events
    assert kernel.module_statuses()[0]["error"] == "module_stop_timeout"

    release_stop.set()
    await asyncio.sleep(0)
    assert await kernel.stop() == []
    assert kernel.shutdown_complete is True
    assert events[-1] == "provider:close"
    assert kernel.module_statuses()[0]["error"] == "module_start_failed"


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
    class DegradedRoutedModule(RoutedModule):
        optional_provider_names = ("missing",)

    module = DegradedRoutedModule()
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
async def test_kernel_uses_frozen_capabilities_after_lifecycle_mutation() -> None:
    resolved: list[object] = []

    class Dependency(Module):
        name = "dependency"

    class Consumer(Module):
        name = "consumer"
        dependencies = ("dependency",)
        optional_provider_names = ("missing",)

        def on_register(self, context: ModuleContext) -> None:
            self.name = "renamed"
            self.dependencies = ()
            self.optional_provider_names = ()
            resolved.append(context.require_module("dependency"))

    dependency = Dependency()
    consumer = Consumer()
    kernel = Kernel([_loaded(consumer, 0), _loaded(dependency, 1)])

    assert kernel.module_order == ("dependency", "consumer")
    await kernel.start()

    assert resolved
    assert kernel.module_order == ("dependency", "consumer")
    assert [item["name"] for item in kernel.module_statuses()] == [
        "dependency",
        "consumer",
    ]
    assert kernel.get_module("consumer")
    assert consumer.state is ModuleState.DEGRADED
    assert await kernel.stop() == []


@pytest.mark.asyncio
async def test_kernel_uses_snapshot_name_when_registration_mutates_then_fails() -> None:
    stopped: list[str] = []

    class Broken(Module):
        name = "stable"

        def on_register(self, context: ModuleContext) -> None:
            self.name = "renamed"
            raise RuntimeError("private registration detail")

        async def on_stop(self, context: ModuleContext) -> None:
            stopped.append("stable")

    kernel = Kernel([_loaded(Broken(), 0)])

    with pytest.raises(
        KernelStartupError, match="Required module registration failed: stable"
    ):
        await kernel.start()

    assert stopped == ["stable"]
    assert kernel.module_statuses() == [
        {
            "name": "stable",
            "required": True,
            "state": "failed",
            "error": "module_registration_failed",
        }
    ]


@pytest.mark.asyncio
async def test_degraded_required_dependency_degrades_consumer_without_blocking() -> (
    None
):
    events: list[str] = []
    dependency = RecordingModule(
        "dependency", events, optional_provider_names=("missing",)
    )
    consumer = RecordingModule("consumer", events, dependencies=("dependency",))
    kernel = Kernel([_loaded(dependency, 0), _loaded(consumer, 1)])

    await kernel.start()

    assert dependency.state is ModuleState.DEGRADED
    assert consumer.state is ModuleState.DEGRADED
    assert "start:consumer" in events
    await kernel.stop()


@pytest.mark.asyncio
async def test_on_register_can_cache_a_registered_dependency_proxy() -> None:
    observed: list[str] = []

    class DependencyApi(Protocol):
        def value(self) -> str: ...

    class Dependency(Module):
        name = "dependency"
        api_contract = DependencyApi

        def value(self) -> str:
            return "ready"

    class Consumer(Module):
        name = "consumer"
        dependencies = ("dependency",)

        def on_register(self, context: ModuleContext) -> None:
            self.dependency = context.require_module("dependency")

        async def on_start(self, context: ModuleContext) -> None:
            observed.append(self.dependency.value())

    kernel = Kernel([_loaded(Dependency(), 0), _loaded(Consumer(), 1)])

    await kernel.start()

    assert observed == ["ready"]
    await kernel.stop()


@pytest.mark.asyncio
async def test_registered_dependency_proxy_is_not_callable_during_registration() -> (
    None
):
    class DependencyApi(Protocol):
        def value(self) -> str: ...

    class Dependency(Module):
        name = "dependency"
        api_contract = DependencyApi

        def value(self) -> str:
            return "not-yet-ready"

    class Consumer(Module):
        name = "consumer"
        dependencies = ("dependency",)

        def on_register(self, context: ModuleContext) -> None:
            context.require_module("dependency").value()

    consumer = Consumer()
    kernel = Kernel([_loaded(Dependency(), 0), _loaded(consumer, 1)])

    with pytest.raises(KernelStartupError):
        await kernel.start()

    assert consumer.state is ModuleState.FAILED
    assert kernel.module_statuses()[1]["error"] == "module_registration_failed"


@pytest.mark.asyncio
async def test_registration_hook_self_cancellation_remains_failed_after_cleanup() -> (
    None
):
    events: list[str] = []

    class CancelledRegistration(Module):
        name = "cancelled_registration"

        def on_register(self, context: ModuleContext) -> None:
            raise asyncio.CancelledError

        async def on_stop(self, context: ModuleContext) -> None:
            events.append("stop:cancelled_registration")

    module = CancelledRegistration()
    kernel = Kernel([_loaded(module, 0)])

    with pytest.raises(asyncio.CancelledError):
        await kernel.start()

    assert events == ["stop:cancelled_registration"]
    assert module.state is ModuleState.FAILED
    assert kernel.module_statuses()[0]["error"] == "module_registration_cancelled"
    assert await kernel.stop() == []


@pytest.mark.asyncio
async def test_on_register_resolves_only_present_optional_dependencies() -> None:
    observed: list[object | None] = []

    class OptionalDependency(Module):
        name = "optional"

    class Consumer(Module):
        name = "consumer"
        optional_dependencies = ("optional", "missing")

        def on_register(self, context: ModuleContext) -> None:
            observed.extend(
                (
                    context.optional_module("optional"),
                    context.optional_module("missing"),
                )
            )

    kernel = Kernel([_loaded(OptionalDependency(), 0), _loaded(Consumer(), 1)])

    await kernel.start()

    assert observed[0] is not None
    assert observed[1] is None
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

    first = RecordingModule("alpha", events)
    broken = BrokenRegistration("broken", events)
    kernel = Kernel([_loaded(first, 0), _loaded(broken, 1)])

    with pytest.raises(KernelStartupError):
        await kernel.start()
    assert events == [
        "register:alpha",
        "register:broken",
        "stop:broken",
        "stop:alpha",
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
