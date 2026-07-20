"""Dynamic module loader with strict extension-boundary validation."""

from __future__ import annotations

import importlib
import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from nexus_kernel.config import ModuleDefinition, ModulesSettings
from nexus_kernel.errors import ConfigError
from nexus_kernel.kernel.module import (
    Module,
    ModuleConfig,
    _ModuleCapabilities,
    _module_capabilities,
)


@dataclass(frozen=True, slots=True)
class LoadedModule:
    module: Module
    required: bool
    original_index: int
    manifest_dependencies: tuple[str, ...] | None = None
    manifest_optional_dependencies: tuple[str, ...] | None = None


def _resolve_module_class(class_path: str) -> type[Module]:
    module_path, separator, class_name = class_path.rpartition(".")
    if not separator or not module_path or not class_name:
        raise ImportError("module class path must include a module and class")

    imported = importlib.import_module(module_path)
    candidate = getattr(imported, class_name)
    if not isinstance(candidate, type) or not issubclass(candidate, Module):
        raise TypeError("configured extension is not a Module subclass")
    return candidate


def _validate_class_manifest(
    name: str,
    module_type: type[Module],
    definition: ModuleDefinition,
) -> _ModuleCapabilities:
    if module_type.name != name:
        raise ValueError(f"module class name does not match: {name}")
    capabilities = _module_capabilities(name, module_type)
    if set(capabilities.dependencies) != set(definition.dependencies) or set(
        capabilities.optional_dependencies
    ) != set(definition.optional_dependencies):
        raise ValueError(f"module dependency manifest does not match: {name}")
    return capabilities


def _validate_config_schema(name: str, module_type: type[Module]) -> None:
    schema = module_type.config_schema
    if not isinstance(schema, type) or not issubclass(schema, ModuleConfig):
        raise TypeError(f"module configuration schema is invalid: {name}")
    config = schema.model_config
    if (
        config.get("extra") != "forbid"
        or config.get("strict") is not True
        or config.get("frozen") is not True
    ):
        raise TypeError(f"module configuration schema is not strict: {name}")


def _validate_instance_manifest(
    name: str,
    module: Module,
    definition: ModuleDefinition,
    class_capabilities: _ModuleCapabilities,
) -> None:
    capabilities = _module_capabilities(name, module)
    if (
        capabilities != class_capabilities
        or set(capabilities.dependencies) != set(definition.dependencies)
        or set(capabilities.optional_dependencies)
        != set(definition.optional_dependencies)
    ):
        raise ValueError(f"module capability declarations do not match: {name}")


def _cycle_path(dependencies: Mapping[str, set[str]]) -> tuple[str, ...]:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    positions: dict[str, int] = {}

    def visit(name: str) -> tuple[str, ...] | None:
        visiting.add(name)
        positions[name] = len(stack)
        stack.append(name)
        for dependency in sorted(dependencies[name]):
            if dependency in visiting:
                start = positions[dependency]
                return (*stack[start:], dependency)
            if dependency not in visited:
                found = visit(dependency)
                if found is not None:
                    return found
        stack.pop()
        positions.pop(name)
        visiting.remove(name)
        visited.add(name)
        return None

    for name in sorted(dependencies):
        if name not in visited:
            found = visit(name)
            if found is not None:
                return found
    return ()


def _resolve_manifest_order(settings: ModulesSettings) -> tuple[str, ...]:
    enabled = {
        name: definition
        for name, definition in settings.modules.items()
        if definition.enabled
    }
    dependencies: dict[str, set[str]] = {name: set() for name in enabled}
    dependents: dict[str, set[str]] = {name: set() for name in enabled}
    indegree = {name: 0 for name in enabled}

    for name, definition in enabled.items():
        for dependency in definition.dependencies:
            configured = settings.modules.get(dependency)
            if configured is None or not configured.enabled:
                raise ConfigError(
                    "Required module dependency is missing or disabled: "
                    f"{name} -> {dependency}"
                )
            dependencies[name].add(dependency)
        dependencies[name].update(
            dependency
            for dependency in definition.optional_dependencies
            if dependency in enabled
        )
        for dependency in dependencies[name]:
            dependents[dependency].add(name)
            indegree[name] += 1

    ready = list(name for name, count in indegree.items() if count == 0)
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        ordered.append(name)
        for dependent in sorted(dependents[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)

    if len(ordered) != len(enabled):
        cycle = _cycle_path(dependencies)
        raise ConfigError(f"Module dependency cycle detected: {' -> '.join(cycle)}")
    return tuple(ordered)


def _prepare_modules(
    settings: ModulesSettings, ordered_names: Sequence[str]
) -> tuple[
    dict[str, type[Module]],
    dict[str, ModuleConfig],
    dict[str, _ModuleCapabilities],
]:
    module_types: dict[str, type[Module]] = {}
    validated_configs: dict[str, ModuleConfig] = {}
    capabilities: dict[str, _ModuleCapabilities] = {}
    for name in ordered_names:
        definition = settings.modules[name]
        try:
            module_type = _resolve_module_class(definition.class_path)
            class_capabilities = _validate_class_manifest(name, module_type, definition)
            _validate_config_schema(name, module_type)
            validated_config = module_type.config_schema.model_validate(
                definition.config
            )
        except Exception:
            raise ConfigError(f"Unable to load configured module: {name}") from None
        module_types[name] = module_type
        validated_configs[name] = validated_config
        capabilities[name] = class_capabilities
    return module_types, validated_configs, capabilities


def _construct_module(
    name: str,
    definition: ModuleDefinition,
    module_type: type[Module],
    validated_config: ModuleConfig,
    class_capabilities: _ModuleCapabilities,
) -> Module:
    try:
        module = module_type()
        if module.name != name:
            raise ValueError(
                "module instance name does not match its configuration key"
            )
        _validate_instance_manifest(name, module, definition, class_capabilities)
        module._set_config(validated_config)
        module.validate_api_implementation()
        return module
    except Exception:
        raise ConfigError(f"Unable to load configured module: {name}") from None


def load_modules(settings: ModulesSettings) -> list[LoadedModule]:
    """Load enabled modules in stable dependency-first order."""

    ordered_names = _resolve_manifest_order(settings)
    module_types, validated_configs, capabilities = _prepare_modules(
        settings, ordered_names
    )
    original_indices = {name: index for index, name in enumerate(settings.modules)}
    loaded: list[LoadedModule] = []
    for name in ordered_names:
        definition = settings.modules[name]
        loaded.append(
            LoadedModule(
                module=_construct_module(
                    name,
                    definition,
                    module_types[name],
                    validated_configs[name],
                    capabilities[name],
                ),
                required=definition.required,
                original_index=original_indices[name],
                manifest_dependencies=tuple(definition.dependencies),
                manifest_optional_dependencies=tuple(definition.optional_dependencies),
            )
        )
    return loaded
