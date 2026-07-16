"""Stable module, configuration, and capability contracts for extensions."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from nexus_kernel.errors import ModuleDependencyError
from nexus_kernel.runtime.workers import WorkerSpec


_HTTP_METHOD = re.compile(r"^[A-Z]+$")


@dataclass(frozen=True, slots=True)
class HttpEndpointSpec:
    path: str
    methods: tuple[str, ...]
    target_api: str
    request_schema: Any = None
    response_schema: Any = None
    status_code: int = 200
    tags: tuple[str, ...] = ()
    auth_required: bool = False
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError("endpoint path must start with '/'")
        if not self.methods:
            raise ValueError("endpoint methods must not be empty")
        if any(not _HTTP_METHOD.fullmatch(method) for method in self.methods):
            raise ValueError("endpoint methods must use uppercase HTTP tokens")
        if not self.target_api.strip():
            raise ValueError("endpoint target_api must not be empty")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("endpoint timeout_seconds must be positive")


class ModuleState(StrEnum):
    CREATED = "created"
    REGISTERED = "registered"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    FAILED = "failed"


class ModuleConfig(BaseModel):
    """Strict immutable base for one module's private configuration schema."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ModuleContext:
    """Capability-limited access to resources declared by one module."""

    __slots__ = (
        "__module_name",
        "__module_resolver",
        "__optional_module_resolver",
        "__optional_modules",
        "__optional_provider_resolver",
        "__optional_providers",
        "__provider_resolver",
        "__required_modules",
        "__required_providers",
    )

    def __init__(
        self,
        *,
        module_name: str,
        required_modules: tuple[str, ...],
        optional_modules: tuple[str, ...],
        required_providers: tuple[str, ...],
        optional_providers: tuple[str, ...],
        module_resolver: Callable[[str], Any],
        optional_module_resolver: Callable[[str], Any | None],
        provider_resolver: Callable[[str], Any],
        optional_provider_resolver: Callable[[str], Any | None],
    ) -> None:
        self.__module_name = module_name
        self.__required_modules = frozenset(required_modules)
        self.__optional_modules = frozenset(optional_modules)
        self.__required_providers = frozenset(required_providers)
        self.__optional_providers = frozenset(optional_providers)
        self.__module_resolver = module_resolver
        self.__optional_module_resolver = optional_module_resolver
        self.__provider_resolver = provider_resolver
        self.__optional_provider_resolver = optional_provider_resolver

    @classmethod
    def _create(
        cls,
        *,
        module_name: str,
        required_modules: tuple[str, ...],
        optional_modules: tuple[str, ...],
        required_providers: tuple[str, ...],
        optional_providers: tuple[str, ...],
        module_resolver: Callable[[str], Any],
        optional_module_resolver: Callable[[str], Any | None],
        provider_resolver: Callable[[str], Any],
        optional_provider_resolver: Callable[[str], Any | None],
    ) -> ModuleContext:
        return cls(
            module_name=module_name,
            required_modules=required_modules,
            optional_modules=optional_modules,
            required_providers=required_providers,
            optional_providers=optional_providers,
            module_resolver=module_resolver,
            optional_module_resolver=optional_module_resolver,
            provider_resolver=provider_resolver,
            optional_provider_resolver=optional_provider_resolver,
        )

    def require_module(self, name: str) -> Any:
        self.__ensure_declared(name, self.__required_modules, "module")
        return self.__module_resolver(name)

    def optional_module(self, name: str) -> Any | None:
        self.__ensure_declared(name, self.__optional_modules, "module")
        return self.__optional_module_resolver(name)

    def require_provider(self, name: str) -> Any:
        self.__ensure_declared(name, self.__required_providers, "provider")
        return self.__provider_resolver(name)

    def optional_provider(self, name: str) -> Any | None:
        self.__ensure_declared(name, self.__optional_providers, "provider")
        return self.__optional_provider_resolver(name)

    def __ensure_declared(
        self, name: str, allowed: frozenset[str], resource_kind: str
    ) -> None:
        if name not in allowed:
            raise ModuleDependencyError(
                f"Module {resource_kind} is not declared for {self.__module_name}: {name}"
            )


class Module:
    """Base class for independently loadable NexusKernel modules."""

    name: str = ""
    version: str = "0.2.0"
    dependencies: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ()
    api_contract: type[Any] | None = None
    http_endpoints: tuple[HttpEndpointSpec, ...] = ()
    required_provider_names: tuple[str, ...] = ()
    optional_provider_names: tuple[str, ...] = ()
    config_schema: type[ModuleConfig] = ModuleConfig

    def __init__(self) -> None:
        self._config = self.config_schema()
        self.state = ModuleState.CREATED

    def _set_config(self, config: ModuleConfig) -> None:
        if not isinstance(config, self.config_schema):
            raise TypeError("module configuration does not match its declared schema")
        self._config = config

    @property
    def config(self) -> ModuleConfig:
        return self._config

    def on_register(self, context: ModuleContext) -> None:
        """Run after dependency injection; default modules need no registration."""

    async def on_start(self, context: ModuleContext) -> None:
        """Start module resources; default modules have none."""

    async def on_stop(self, context: ModuleContext) -> None:
        """Stop module resources; default modules have none."""

    def get_api_contract(self) -> list[str]:
        if self.api_contract is None:
            return []

        names: list[str] = []
        for contract in reversed(self.api_contract.__mro__):
            for name, value in vars(contract).items():
                if name.startswith("_") or not callable(value) or name in names:
                    continue
                names.append(name)
        return names

    def get_http_endpoints(self) -> list[HttpEndpointSpec]:
        return list(self.http_endpoints)

    def validate_api_implementation(self) -> None:
        for name in self.get_api_contract():
            contract_callable = getattr(self.api_contract, name, None)
            implementation = getattr(type(self), name, None)
            if (
                not callable(contract_callable)
                or not callable(implementation)
                or not self._signatures_match(contract_callable, implementation)
                or inspect.iscoroutinefunction(contract_callable)
                is not inspect.iscoroutinefunction(implementation)
            ):
                raise ModuleDependencyError(
                    f"Module API implementation is invalid: {self.name}.{name}"
                )

    @staticmethod
    def _signatures_match(contract: Callable[..., Any], implementation: Any) -> bool:
        try:
            expected = tuple(inspect.signature(contract).parameters.values())
            actual = tuple(inspect.signature(implementation).parameters.values())
        except (TypeError, ValueError):
            return False
        if len(expected) != len(actual):
            return False
        return all(
            expected_parameter.name == actual_parameter.name
            and expected_parameter.kind is actual_parameter.kind
            and (expected_parameter.default is inspect.Parameter.empty)
            is (actual_parameter.default is inspect.Parameter.empty)
            for expected_parameter, actual_parameter in zip(
                expected, actual, strict=True
            )
        )

    def get_worker_specs(self) -> list[WorkerSpec]:
        return []

    def get_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "dependencies": list(self.dependencies),
            "optional_dependencies": list(self.optional_dependencies),
            "api_contract": self.get_api_contract(),
            "http_endpoints": len(self.http_endpoints),
            "required_provider_names": list(self.required_provider_names),
            "optional_provider_names": list(self.optional_provider_names),
            "state": self.state.value,
        }
