"""Stable module and endpoint contracts for NexusKernel extensions."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nexuskernel.errors import ModuleDependencyError


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


class ProviderState(StrEnum):
    REGISTERED = "registered"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    STOPPED = "stopped"
    FAILED = "failed"


class ModuleConfig:
    """Read-only, defensively copied configuration bound to one module."""

    __slots__ = ("_data", "name")

    def __init__(self, name: str, data: Mapping[str, Any] | None = None) -> None:
        self.name = name
        self._data = copy.deepcopy(dict(data or {}))

    def get(self, key: str, default: Any = None) -> Any:
        current: Any = self._data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return copy.deepcopy(default)
            current = current[part]
        return copy.deepcopy(current)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


class Module:
    """Base class for independently loadable NexusKernel modules."""

    name: str = ""
    version: str = "0.1.0"
    dependencies: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ()
    api_contract: type[Any] | None = None
    http_endpoints: tuple[HttpEndpointSpec, ...] = ()
    required_provider_names: tuple[str, ...] = ()
    optional_provider_names: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._kernel: Any = None
        self._config: ModuleConfig | None = None
        self.state = ModuleState.CREATED

    def bind_kernel(self, kernel: Any) -> None:
        self._kernel = kernel

    def bind_config(self, config: ModuleConfig | Mapping[str, Any]) -> None:
        self._config = (
            config
            if isinstance(config, ModuleConfig)
            else ModuleConfig(self.name, config)
        )

    @property
    def config(self) -> ModuleConfig | None:
        return self._config

    def on_register(self, kernel: Any) -> None:
        """Run after dependency injection; default modules need no registration."""

    async def on_start(self, kernel: Any) -> None:
        """Start module resources; default modules have none."""

    async def on_stop(self, kernel: Any) -> None:
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

    def get_dependency(self, name: str) -> Any:
        if self._kernel is None:
            raise ModuleDependencyError("Module is not bound to a kernel")
        return self._kernel.get_module(name)

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
