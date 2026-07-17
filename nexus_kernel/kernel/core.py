"""Dependency-aware Kernel lifecycle and capability-limited module access."""

from __future__ import annotations

import asyncio
import heapq
import inspect
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any

from nexus_kernel.errors import (
    KernelStartupError,
    ModuleAccessError,
    ModuleDependencyError,
    ModuleLookupError,
    WorkerError,
)
from nexus_kernel.kernel.loader import LoadedModule
from nexus_kernel.kernel.module import (
    HttpEndpointSpec,
    Module,
    ModuleContext,
    ModuleState,
)
from nexus_kernel.runtime.workers import WorkerSpec

if TYPE_CHECKING:
    from nexus_kernel.providers import ProviderRegistry


_OPERATIONAL_STATES = frozenset({ModuleState.READY, ModuleState.DEGRADED})
_RESOLVABLE_STATES = frozenset(
    {ModuleState.REGISTERED, ModuleState.READY, ModuleState.DEGRADED}
)


@dataclass(frozen=True, slots=True)
class ModuleRouteBinding:
    module_name: str
    spec: HttpEndpointSpec
    handler: Callable[..., Any]


@dataclass(slots=True)
class _ModuleCleanup:
    task: asyncio.Task[None]
    prior_state: ModuleState
    prior_error: str | None
    cancel_requested: bool = False


class ModuleApiProxy:
    """Expose only Protocol callables and guard every invocation by module state."""

    __slots__ = ("__allowed", "__module_name", "__target")

    def __init__(self, module: Module) -> None:
        object.__setattr__(self, "_ModuleApiProxy__target", module)
        object.__setattr__(self, "_ModuleApiProxy__module_name", module.name)
        object.__setattr__(
            self,
            "_ModuleApiProxy__allowed",
            frozenset(module.get_api_contract()),
        )

    def __getattribute__(self, name: str) -> Any:
        if name in {"__class__", "__dir__"}:
            return object.__getattribute__(self, name)
        allowed = object.__getattribute__(self, "_ModuleApiProxy__allowed")
        module_name = object.__getattribute__(self, "_ModuleApiProxy__module_name")
        if name not in allowed:
            raise ModuleAccessError(
                f"Module API does not expose attribute: {module_name}.{name}"
            )
        target = object.__getattribute__(self, "_ModuleApiProxy__target")
        implementation = getattr(target, name)

        if inspect.iscoroutinefunction(implementation):

            @wraps(implementation)
            async def guarded_async(*args: Any, **kwargs: Any) -> Any:
                ModuleApiProxy._ensure_operational(target, module_name)
                return await getattr(target, name)(*args, **kwargs)

            return guarded_async

        @wraps(implementation)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            ModuleApiProxy._ensure_operational(target, module_name)
            return getattr(target, name)(*args, **kwargs)

        return guarded

    @staticmethod
    def _ensure_operational(target: Module, module_name: str) -> None:
        if target.state not in _OPERATIONAL_STATES:
            raise ModuleDependencyError(f"Module API is unavailable: {module_name}")

    def __dir__(self) -> list[str]:
        allowed = object.__getattribute__(self, "_ModuleApiProxy__allowed")
        return sorted(allowed)


class Kernel:
    """Own Provider and Module startup, rollback, health, and shutdown ordering."""

    def __init__(
        self,
        modules: Iterable[LoadedModule],
        providers: ProviderRegistry | None = None,
        *,
        module_cleanup_timeout_seconds: float = 5.0,
    ) -> None:
        if (
            not math.isfinite(module_cleanup_timeout_seconds)
            or module_cleanup_timeout_seconds <= 0
        ):
            raise ValueError("module cleanup timeout must be a positive finite value")
        entries = list(modules)
        self._entries = self._index_entries(entries)
        self._ordered = self._resolve_topology(entries)
        if providers is None:
            from nexus_kernel.providers import ProviderRegistry

            providers = ProviderRegistry()
        self._providers = providers
        self._contexts = {
            entry.module.name: self._create_context(entry.module) for entry in entries
        }
        self._worker_specs = self._index_worker_specs()
        self._cleanup_pending: list[LoadedModule] = []
        self._cleanup_tasks: dict[str, _ModuleCleanup] = {}
        self._cleaned: set[str] = set()
        self._module_errors: dict[str, str | None] = {
            entry.module.name: None for entry in entries
        }
        self._running = False
        self._stopped = False
        self._module_cleanup_timeout_seconds = module_cleanup_timeout_seconds
        self._lifecycle_lock = asyncio.Lock()

    @staticmethod
    def _index_entries(entries: list[LoadedModule]) -> dict[str, LoadedModule]:
        indexed: dict[str, LoadedModule] = {}
        for entry in entries:
            entry.module.validate_api_implementation()
            name = entry.module.name
            if name in indexed:
                raise ModuleDependencyError(f"Module name is duplicated: {name}")
            indexed[name] = entry
        return indexed

    def _resolve_topology(self, entries: list[LoadedModule]) -> list[LoadedModule]:
        by_name = self._entries
        indegree = {name: 0 for name in by_name}
        dependents: dict[str, set[str]] = {name: set() for name in by_name}

        for entry in entries:
            module = entry.module
            for dependency in module.dependencies:
                if dependency not in by_name:
                    raise ModuleDependencyError(
                        f"Required module dependency is missing: {dependency}"
                    )
            present_dependencies = set(module.dependencies)
            present_dependencies.update(
                name for name in module.optional_dependencies if name in by_name
            )
            for dependency in present_dependencies:
                if module.name not in dependents[dependency]:
                    dependents[dependency].add(module.name)
                    indegree[module.name] += 1

        ready = [
            (entry.original_index, entry.module.name)
            for entry in entries
            if indegree[entry.module.name] == 0
        ]
        heapq.heapify(ready)
        ordered: list[LoadedModule] = []
        while ready:
            _, name = heapq.heappop(ready)
            ordered.append(by_name[name])
            for dependent in sorted(
                dependents[name], key=lambda item: by_name[item].original_index
            ):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(
                        ready, (by_name[dependent].original_index, dependent)
                    )

        if len(ordered) != len(entries):
            cyclic = sorted(name for name, count in indegree.items() if count > 0)
            raise ModuleDependencyError(
                f"Module dependency cycle detected: {', '.join(cyclic)}"
            )
        return ordered

    def _create_context(self, module: Module) -> ModuleContext:
        return ModuleContext._create(
            module_name=module.name,
            required_modules=module.dependencies,
            optional_modules=module.optional_dependencies,
            required_providers=module.required_provider_names,
            optional_providers=module.optional_provider_names,
            module_resolver=self._require_resolvable_module,
            optional_module_resolver=self._optional_resolvable_module,
            provider_resolver=self._providers.require,
            optional_provider_resolver=self._providers.get,
        )

    def _index_worker_specs(self) -> dict[str, tuple[WorkerSpec, ...]]:
        indexed: dict[str, tuple[WorkerSpec, ...]] = {}
        owners: dict[str, str] = {}
        for entry in self._ordered:
            try:
                specs = tuple(entry.module.get_worker_specs())
            except Exception:
                raise WorkerError(
                    f"Unable to load Worker specs for module: {entry.module.name}"
                ) from None
            for spec in specs:
                if not isinstance(spec, WorkerSpec):
                    raise WorkerError(
                        f"Module returned an invalid Worker spec: {entry.module.name}"
                    )
                previous_owner = owners.get(spec.name)
                if previous_owner is not None:
                    raise WorkerError(
                        "Worker name is duplicated across modules: "
                        f"{spec.name} ({previous_owner}, {entry.module.name})"
                    )
                owners[spec.name] = entry.module.name
            indexed[entry.module.name] = specs
        return indexed

    @property
    def module_order(self) -> tuple[str, ...]:
        return tuple(entry.module.name for entry in self._ordered)

    @property
    def providers(self) -> ProviderRegistry:
        return self._providers

    @property
    def shutdown_complete(self) -> bool:
        """Return whether every Module and Provider has reached a terminal stop."""

        return self._stopped

    def get_module(self, name: str) -> ModuleApiProxy:
        try:
            module = self._entries[name].module
        except KeyError:
            raise ModuleLookupError(f"Module is not registered: {name}") from None
        return ModuleApiProxy(module)

    def _require_resolvable_module(self, name: str) -> ModuleApiProxy:
        proxy = self.get_module(name)
        if self._entries[name].module.state not in _RESOLVABLE_STATES:
            raise ModuleDependencyError(f"Required module is unavailable: {name}")
        return proxy

    def _optional_resolvable_module(self, name: str) -> ModuleApiProxy | None:
        entry = self._entries.get(name)
        if entry is None or entry.module.state not in _RESOLVABLE_STATES:
            return None
        return ModuleApiProxy(entry.module)

    def route_bindings(self) -> list[ModuleRouteBinding]:
        """Return declared endpoint specs paired with state-guarded handlers."""

        bindings: list[ModuleRouteBinding] = []
        for entry in self._ordered:
            proxy = self.get_module(entry.module.name)
            for endpoint in entry.module.get_http_endpoints():
                try:
                    handler = getattr(proxy, endpoint.target_api)
                except ModuleAccessError:
                    raise ModuleDependencyError(
                        "HTTP endpoint target is outside its module API contract: "
                        f"{entry.module.name}.{endpoint.target_api}"
                    ) from None
                bindings.append(
                    ModuleRouteBinding(entry.module.name, endpoint, handler)
                )
        return bindings

    def worker_specs(self) -> list[WorkerSpec]:
        return [
            spec
            for entry in self._ordered
            if entry.module.state in _OPERATIONAL_STATES
            for spec in self._worker_specs[entry.module.name]
        ]

    async def start(self) -> None:
        async with self._lifecycle_lock:
            await self._start_once()

    async def _start_once(self) -> None:
        if self._running:
            return
        if self._stopped:
            raise KernelStartupError("A stopped Kernel cannot be restarted")

        try:
            await self._providers.initialize_all()
            await self._register_modules()
            for entry in self._ordered:
                if entry.module.state is ModuleState.FAILED:
                    continue
                await self._start_module(entry)
        except asyncio.CancelledError as cancellation:
            await self._rollback_startup()
            raise cancellation
        except KernelStartupError:
            cleanup_cancellation = await self._rollback_startup()
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            raise
        except Exception:
            cleanup_cancellation = await self._rollback_startup()
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            raise KernelStartupError("Kernel startup failed") from None
        self._running = True

    async def _register_modules(self) -> None:
        for entry in self._ordered:
            module = entry.module
            self._cleanup_pending.append(entry)
            try:
                module.on_register(self._contexts[module.name])
            except asyncio.CancelledError:
                module.state = ModuleState.FAILED
                self._module_errors[module.name] = "module_registration_cancelled"
                raise
            except Exception:
                module.state = ModuleState.FAILED
                self._module_errors[module.name] = "module_registration_failed"
                _, cleanup_cancellation, _ = await self._cleanup_module(entry)
                if cleanup_cancellation is not None:
                    raise cleanup_cancellation
                if entry.required:
                    raise KernelStartupError(
                        f"Required module registration failed: {module.name}"
                    ) from None
            else:
                module.state = ModuleState.REGISTERED

    async def _start_module(self, entry: LoadedModule) -> None:
        module = entry.module
        unavailable_dependency = next(
            (
                name
                for name in module.dependencies
                if self._entries[name].module.state not in _OPERATIONAL_STATES
            ),
            None,
        )
        if unavailable_dependency is not None:
            await self._module_failed(entry, "module_dependency_unavailable")
            return

        degraded = any(
            self._entries[name].module.state is ModuleState.DEGRADED
            for name in module.dependencies
        ) or any(
            name not in self._entries
            or self._entries[name].module.state is not ModuleState.READY
            for name in module.optional_dependencies
        )

        try:
            for provider_name in module.required_provider_names:
                self._providers.require(provider_name)
            optional_provider_missing = any(
                self._providers.get(name) is None
                for name in module.optional_provider_names
            )
            degraded = degraded or optional_provider_missing
            module.state = ModuleState.STARTING
            await module.on_start(self._contexts[module.name])
        except asyncio.CancelledError as cancellation:
            module.state = ModuleState.FAILED
            self._module_errors[module.name] = "module_start_cancelled"
            await self._cleanup_module(entry)
            raise cancellation
        except Exception:
            await self._module_failed(entry, "module_start_failed")
            return

        module.state = ModuleState.DEGRADED if degraded else ModuleState.READY
        self._module_errors[module.name] = (
            "optional_capability_unavailable" if degraded else None
        )

    async def _module_failed(self, entry: LoadedModule, error_code: str) -> None:
        module = entry.module
        module.state = ModuleState.FAILED
        self._module_errors[module.name] = error_code
        _, cleanup_cancellation, _ = await self._cleanup_module(entry)
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if entry.required:
            raise KernelStartupError(
                f"Required module failed to start: {module.name}"
            ) from None

    async def _wait_for_module_cleanup(
        self,
        cleanup: _ModuleCleanup,
        *,
        allow_retry: bool,
    ) -> tuple[bool, bool, asyncio.CancelledError | None]:
        task = cleanup.task
        if cleanup.cancel_requested and not task.done() and not allow_retry:
            return False, False, None

        deadline = (
            asyncio.get_running_loop().time() + self._module_cleanup_timeout_seconds
        )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                done, _ = await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                continue
            if not done:
                break

        if not task.done():
            if not cleanup.cancel_requested:
                cleanup.cancel_requested = True
                task.cancel()
            return False, False, cancellation

        try:
            task.result()
        except BaseException:
            return True, False, cancellation
        return True, True, cancellation

    @staticmethod
    def _consume_module_cleanup_result(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    async def _cleanup_module(
        self,
        entry: LoadedModule,
        *,
        allow_retry: bool = True,
    ) -> tuple[str | None, asyncio.CancelledError | None, bool]:
        module = entry.module
        if module.name in self._cleaned:
            return None, None, False
        cleanup = self._cleanup_tasks.get(module.name)
        if cleanup is None:
            task = asyncio.create_task(
                module.on_stop(self._contexts[module.name]),
                name=f"nexus_kernel_module_cleanup_{module.name}",
            )
            task.add_done_callback(self._consume_module_cleanup_result)
            cleanup = _ModuleCleanup(
                task=task,
                prior_state=module.state,
                prior_error=self._module_errors[module.name],
            )
            self._cleanup_tasks[module.name] = cleanup
        terminal, succeeded, cancellation = await self._wait_for_module_cleanup(
            cleanup,
            allow_retry=allow_retry,
        )
        if not terminal:
            module.state = ModuleState.FAILED
            self._module_errors[module.name] = "module_stop_timeout"
            return f"module_stop_timeout:{module.name}", cancellation, True

        self._cleaned.add(module.name)
        if not succeeded:
            module.state = ModuleState.FAILED
            if cleanup.prior_state is ModuleState.FAILED:
                self._module_errors[module.name] = cleanup.prior_error
            else:
                self._module_errors[module.name] = "module_stop_failed"
            return f"module_stop_failed:{module.name}", cancellation, False
        if cleanup.prior_state is not ModuleState.FAILED:
            module.state = ModuleState.STOPPED
            self._module_errors[module.name] = None
        else:
            module.state = ModuleState.FAILED
            self._module_errors[module.name] = cleanup.prior_error
        return None, cancellation, False

    async def _rollback_startup(self) -> asyncio.CancelledError | None:
        _, cancellation, pending = await self._stop_registered_modules(
            allow_retry=False
        )
        if not pending:
            try:
                await self._providers.close_all()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        self._running = False
        self._stopped = not pending
        return cancellation

    async def stop(self) -> list[str]:
        async with self._lifecycle_lock:
            return await self._stop_once()

    async def _stop_once(self) -> list[str]:
        if self._stopped:
            return []
        errors, cancellation, pending = await self._stop_registered_modules()
        self._running = False
        if pending:
            if cancellation is not None:
                raise cancellation
            return errors

        try:
            errors.extend(await self._providers.close_all())
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        self._stopped = True
        if cancellation is not None:
            raise cancellation
        return errors

    async def _stop_registered_modules(
        self,
        *,
        allow_retry: bool = True,
    ) -> tuple[list[str], asyncio.CancelledError | None, bool]:
        errors: list[str] = []
        cancellation: asyncio.CancelledError | None = None
        while self._cleanup_pending:
            entry = self._cleanup_pending[-1]
            error, cleanup_cancellation, pending = await self._cleanup_module(
                entry,
                allow_retry=allow_retry,
            )
            cancellation = cancellation or cleanup_cancellation
            if error is not None:
                errors.append(error)
            if pending:
                return errors, cancellation, True
            self._cleanup_pending.pop()
            if cancellation is not None:
                return errors, cancellation, bool(self._cleanup_pending)
        return errors, cancellation, False

    def module_statuses(self) -> list[dict[str, Any]]:
        return [
            {
                "name": entry.module.name,
                "required": entry.required,
                "state": entry.module.state.value,
                "error": self._module_errors[entry.module.name],
            }
            for entry in self._ordered
        ]

    def health_status(self) -> dict[str, Any]:
        module_statuses = self.module_statuses()
        provider_statuses = self._providers.statuses()
        degraded = any(
            item["state"] in {"degraded", "failed"}
            for item in [*module_statuses, *provider_statuses]
        )
        return {
            "status": "degraded" if degraded else "healthy",
            "modules": module_statuses,
            "providers": provider_statuses,
        }
