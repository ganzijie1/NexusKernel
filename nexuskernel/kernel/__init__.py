"""Stable public module extension interfaces."""

from nexuskernel.kernel.core import Kernel, ModuleApiProxy, ModuleRouteBinding
from nexuskernel.kernel.loader import LoadedModule, load_modules
from nexuskernel.kernel.module import (
    HttpEndpointSpec,
    Module,
    ModuleConfig,
    ModuleState,
    ProviderState,
)

__all__ = [
    "HttpEndpointSpec",
    "Kernel",
    "LoadedModule",
    "Module",
    "ModuleApiProxy",
    "ModuleRouteBinding",
    "ModuleConfig",
    "ModuleState",
    "ProviderState",
    "load_modules",
]
