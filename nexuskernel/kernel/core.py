"""Dependency-aware Kernel lifecycle and contract-limited module access."""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexuskernel.errors import (
    KernelStartupError,
    ModuleAccessError,
    ModuleDependencyError,
    ModuleLookupError,
)
from nexuskernel.kernel.loader import LoadedModule
from nexuskernel.kernel.module import HttpEndpointSpec, Module, ModuleState

if TYPE_CHECKING:
    from nexuskernel.providers import ProviderRegistry


@dataclass(frozen=True, slots=True)
class ModuleRouteBinding:
    module_name: str
    spec: HttpEndpointSpec
    handler: Callable[..., Any]


class ModuleApiProxy:
    """Expose only callables declared by a module's Protocol contract."""

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
        return getattr(target, name)

    def __dir__(self) -> list[str]:
        allowed = object.__getattribute__(self, "_ModuleApiProxy__allowed")
        return sorted(allowed)


class Kernel:
    """Own Provider and Module startup, rollback, health, and shutdown ordering."""

    def __init__(
        self,
        modules: Iterable[LoadedModule],
        providers: ProviderRegistry | None = None,
    ) -> None:
        entries = list(modules)
        self._entries = self._index_entries(entries)
        self._ordered = self._resolve_topology(entries)
        if providers is None:
            from nexuskernel.providers import ProviderRegistry

            providers = ProviderRegistry()
        self._providers = providers
        self._started: list[LoadedModule] = []
        self._module_errors: dict[str, str | None] = {
            entry.module.name: None for entry in entries
        }
        self._running = False
        self._stopped = False

    @staticmethod
    def _index_entries(entries: list[LoadedModule]) -> dict[str, LoadedModule]:
        indexed: dict[str, LoadedModule] = {}
        for entry in entries:
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

    @property
    def module_order(self) -> tuple[str, ...]:
        return tuple(entry.module.name for entry in self._ordered)

    @property
    def providers(self) -> ProviderRegistry:
        return self._providers

    def get_module(self, name: str) -> ModuleApiProxy:
        try:
            module = self._entries[name].module
        except KeyError as exc:
            raise ModuleLookupError(f"Module is not registered: {name}") from exc
        return ModuleApiProxy(module)

    def route_bindings(self) -> list[ModuleRouteBinding]:
        """Return declared endpoint specs paired with contract-limited handlers."""

        bindings: list[ModuleRouteBinding] = []
        for entry in self._ordered:
            proxy = self.get_module(entry.module.name)
            for endpoint in entry.module.get_http_endpoints():
                try:
                    handler = getattr(proxy, endpoint.target_api)
                except ModuleAccessError as exc:
                    raise ModuleDependencyError(
                        "HTTP endpoint target is outside its module API contract: "
                        f"{entry.module.name}.{endpoint.target_api}"
                    ) from exc
                if not callable(handler):
                    raise ModuleDependencyError(
                        "HTTP endpoint target must be callable: "
                        f"{entry.module.name}.{endpoint.target_api}"
                    )
                bindings.append(
                    ModuleRouteBinding(entry.module.name, endpoint, handler)
                )
        return bindings

    async def start(self) -> None:
        if self._running:
            return
        if self._stopped:
            raise KernelStartupError("A stopped Kernel cannot be restarted")

        try:
            await self._providers.initialize_all()
            self._register_modules()
            for entry in self._ordered:
                if entry.module.state is ModuleState.DEGRADED:
                    continue
                await self._start_module(entry)
        except asyncio.CancelledError:
            await self._rollback_startup()
            raise
        except KernelStartupError:
            await self._rollback_startup()
            raise
        except Exception as exc:
            await self._rollback_startup()
            raise KernelStartupError("Kernel startup failed") from exc
        self._running = True

    def _register_modules(self) -> None:
        for entry in self._ordered:
            module = entry.module
            module.bind_kernel(self)
            try:
                module.on_register(self)
            except Exception as exc:
                module.state = (
                    ModuleState.FAILED if entry.required else ModuleState.DEGRADED
                )
                self._module_errors[module.name] = "module_registration_failed"
                if entry.required:
                    raise KernelStartupError(
                        f"Required module registration failed: {module.name}"
                    ) from exc
            else:
                module.state = ModuleState.REGISTERED

    async def _start_module(self, entry: LoadedModule) -> None:
        module = entry.module
        unavailable_dependency = next(
            (
                name
                for name in module.dependencies
                if self._entries[name].module.state is not ModuleState.READY
            ),
            None,
        )
        if unavailable_dependency is not None:
            await self._module_failed(
                entry,
                "module_dependency_unavailable",
                KernelStartupError(
                    f"Required dependency is unavailable: {unavailable_dependency}"
                ),
            )
            return

        try:
            for provider_name in module.required_provider_names:
                self._providers.require(provider_name)
            optional_provider_missing = any(
                self._providers.get(name) is None
                for name in module.optional_provider_names
            )
            module.state = ModuleState.STARTING
            await module.on_start(self)
        except asyncio.CancelledError:
            module.state = ModuleState.FAILED
            self._module_errors[module.name] = "module_start_cancelled"
            await self._cleanup_failed_module(module)
            raise
        except Exception as exc:
            await self._module_failed(entry, "module_start_failed", exc)
            return

        module.state = (
            ModuleState.DEGRADED if optional_provider_missing else ModuleState.READY
        )
        self._module_errors[module.name] = (
            "optional_provider_unavailable" if optional_provider_missing else None
        )
        self._started.append(entry)

    async def _module_failed(
        self, entry: LoadedModule, error_code: str, cause: Exception
    ) -> None:
        module = entry.module
        module.state = ModuleState.FAILED if entry.required else ModuleState.DEGRADED
        self._module_errors[module.name] = error_code
        await self._cleanup_failed_module(module)
        if entry.required:
            raise KernelStartupError(
                f"Required module failed to start: {module.name}"
            ) from cause

    async def _cleanup_failed_module(self, module: Module) -> None:
        try:
            await module.on_stop(self)
        except Exception:
            pass

    async def _rollback_startup(self) -> None:
        await self._stop_started_modules()
        await self._providers.close_all()
        self._running = False
        self._stopped = True

    async def stop(self) -> list[str]:
        if self._stopped:
            return []
        errors = await self._stop_started_modules()
        errors.extend(await self._providers.close_all())
        self._running = False
        self._stopped = True
        return errors

    async def _stop_started_modules(self) -> list[str]:
        errors: list[str] = []
        for entry in reversed(self._started):
            module = entry.module
            try:
                await module.on_stop(self)
            except asyncio.CancelledError:
                raise
            except Exception:
                module.state = ModuleState.FAILED
                self._module_errors[module.name] = "module_stop_failed"
                errors.append(f"module_stop_failed:{module.name}")
            else:
                module.state = ModuleState.STOPPED
                self._module_errors[module.name] = None
        self._started.clear()
        return errors

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
