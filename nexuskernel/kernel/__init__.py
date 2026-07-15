"""Stable public module extension interfaces."""

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
    "LoadedModule",
    "Module",
    "ModuleConfig",
    "ModuleState",
    "ProviderState",
    "load_modules",
]
