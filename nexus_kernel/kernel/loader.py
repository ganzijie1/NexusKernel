"""Dynamic module loader with strict extension-boundary validation."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from nexus_kernel.config import ModuleDefinition, ModulesSettings
from nexus_kernel.errors import ConfigError
from nexus_kernel.kernel.module import Module


@dataclass(frozen=True, slots=True)
class LoadedModule:
    module: Module
    required: bool
    original_index: int


def _resolve_module_class(class_path: str) -> type[Module]:
    module_path, separator, class_name = class_path.rpartition(".")
    if not separator or not module_path or not class_name:
        raise ImportError("module class path must include a module and class")

    imported = importlib.import_module(module_path)
    candidate = getattr(imported, class_name)
    if not isinstance(candidate, type) or not issubclass(candidate, Module):
        raise TypeError("configured extension is not a Module subclass")
    return candidate


def _load_one(name: str, definition: ModuleDefinition) -> Module:
    try:
        module_type = _resolve_module_class(definition.class_path)
        module = module_type()
        if module.name != name:
            raise ValueError(
                "module instance name does not match its configuration key"
            )
        module.validate_api_implementation()
        module._set_config(module.config_schema.model_validate(definition.config))
        return module
    except Exception:
        raise ConfigError(f"Unable to load configured module: {name}") from None


def load_modules(settings: ModulesSettings) -> list[LoadedModule]:
    """Load enabled modules in their original YAML declaration order."""

    loaded: list[LoadedModule] = []
    for index, (name, definition) in enumerate(settings.modules.items()):
        if not definition.enabled:
            continue
        loaded.append(
            LoadedModule(
                module=_load_one(name, definition),
                required=definition.required,
                original_index=index,
            )
        )
    return loaded
