"""Stable module, configuration, and capability contracts for extensions."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from nexus_kernel._validation import require_positive_finite_number
from nexus_kernel.errors import ModuleDependencyError
from nexus_kernel.runtime.workers import WorkerSpec


_HTTP_METHOD = re.compile(r"^[A-Z]+$")
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_UNBOUND_CONFIG = object()


@dataclass(frozen=True, slots=True)
class _ModuleCapabilities:
    dependencies: tuple[str, ...]
    optional_dependencies: tuple[str, ...]
    required_provider_names: tuple[str, ...]
    optional_provider_names: tuple[str, ...]


def _validate_declaration_tuple(
    value: object,
    *,
    module_name: str,
    field_name: str,
    provider_names: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or (provider_names and _PROVIDER_NAME.fullmatch(name) is None)
        for name in value
    ):
        raise ModuleDependencyError(
            f"Module capability declaration is invalid: {module_name}.{field_name}"
        )
    if len(set(value)) != len(value):
        raise ModuleDependencyError(
            f"Module capability declaration is duplicated: {module_name}.{field_name}"
        )
    return tuple(sorted(value))


def _validate_capability_declarations(
    module_name: str,
    *,
    dependencies: object,
    optional_dependencies: object,
    required_provider_names: object,
    optional_provider_names: object,
) -> _ModuleCapabilities:
    required_modules = _validate_declaration_tuple(
        dependencies,
        module_name=module_name,
        field_name="dependencies",
    )
    optional_modules = _validate_declaration_tuple(
        optional_dependencies,
        module_name=module_name,
        field_name="optional_dependencies",
    )
    required_providers = _validate_declaration_tuple(
        required_provider_names,
        module_name=module_name,
        field_name="required_provider_names",
        provider_names=True,
    )
    optional_providers = _validate_declaration_tuple(
        optional_provider_names,
        module_name=module_name,
        field_name="optional_provider_names",
        provider_names=True,
    )
    if (
        module_name in required_modules
        or module_name in optional_modules
        or set(required_modules).intersection(optional_modules)
        or set(required_providers).intersection(optional_providers)
    ):
        raise ModuleDependencyError(
            f"Module capability declarations are invalid: {module_name}"
        )
    return _ModuleCapabilities(
        dependencies=required_modules,
        optional_dependencies=optional_modules,
        required_provider_names=required_providers,
        optional_provider_names=optional_providers,
    )


def _module_capabilities(module_name: str, source: object) -> _ModuleCapabilities:
    return _validate_capability_declarations(
        module_name,
        dependencies=getattr(source, "dependencies"),
        optional_dependencies=getattr(source, "optional_dependencies"),
        required_provider_names=getattr(source, "required_provider_names"),
        optional_provider_names=getattr(source, "optional_provider_names"),
    )


def _api_descriptor(value: Any) -> tuple[str, Callable[..., Any]] | None:
    if isinstance(value, staticmethod):
        return "static", value.__func__
    if isinstance(value, classmethod):
        return "class", value.__func__
    if inspect.isfunction(value):
        return "instance", value
    return None


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
        if self.timeout_seconds is not None:
            require_positive_finite_number(
                self.timeout_seconds, "endpoint timeout_seconds"
            )


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
        self._config: ModuleConfig | object = _UNBOUND_CONFIG
        self.state = ModuleState.CREATED

    def _set_config(self, config: ModuleConfig) -> None:
        if not isinstance(config, self.config_schema):
            raise TypeError("module configuration does not match its declared schema")
        if self._config is not _UNBOUND_CONFIG:
            raise RuntimeError("module configuration is already bound")
        self._config = config

    @property
    def config(self) -> ModuleConfig:
        if self._config is _UNBOUND_CONFIG:
            self._config = self.config_schema()
        return cast(ModuleConfig, self._config)

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
                if (
                    name.startswith("_")
                    or _api_descriptor(value) is None
                    or name in names
                ):
                    continue
                names.append(name)
        return names

    def get_http_endpoints(self) -> list[HttpEndpointSpec]:
        return list(self.http_endpoints)

    def validate_api_implementation(self) -> None:
        for name in self.get_api_contract():
            contract_descriptor = _api_descriptor(
                inspect.getattr_static(self.api_contract, name, None)
            )
            implementation_descriptor = _api_descriptor(
                inspect.getattr_static(type(self), name, None)
            )
            if (
                contract_descriptor is None
                or implementation_descriptor is None
                or contract_descriptor[0] != implementation_descriptor[0]
                or not self._signatures_match(
                    contract_descriptor[1], implementation_descriptor[1]
                )
                or inspect.iscoroutinefunction(contract_descriptor[1])
                is not inspect.iscoroutinefunction(implementation_descriptor[1])
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
