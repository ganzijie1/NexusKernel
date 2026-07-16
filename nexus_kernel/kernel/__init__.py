"""Stable public module extension interfaces."""

from nexus_kernel.kernel.core import Kernel, ModuleApiProxy, ModuleRouteBinding
from nexus_kernel.kernel.loader import LoadedModule, load_modules
from nexus_kernel.kernel.module import (
    HttpEndpointSpec,
    Module,
    ModuleConfig,
    ModuleContext,
    ModuleState,
)

__all__ = [
    "HttpEndpointSpec",
    "Kernel",
    "LoadedModule",
    "Module",
    "ModuleApiProxy",
    "ModuleRouteBinding",
    "ModuleConfig",
    "ModuleContext",
    "ModuleState",
    "load_modules",
]
