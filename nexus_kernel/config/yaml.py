"""YAML loading with duplicate-key and environment substitution safeguards."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from nexus_kernel.config.models import ApplicationSettings, ModulesSettings
from nexus_kernel.errors import ConfigError


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_FULL_PLACEHOLDER = re.compile(
    r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>.*))?\}$"
)
_EMBEDDED_PLACEHOLDER = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>.*?))?\}"
)
_INTEGER = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)$")
_FLOAT = re.compile(
    r"^[+-]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|"
    r"[0-9]+[eE][+-]?[0-9]+)$"
)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "duplicate YAML key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _lookup_environment(name: str, default: str | None) -> str:
    value = os.environ.get(name)
    if value is None or (default is not None and value == ""):
        if default is None:
            raise ConfigError(f"Required environment variable is missing: {name}")
        return default
    return value


def _convert_scalar(value: str) -> str | bool | int | float:
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        parsed = value
    if isinstance(parsed, (dict, list)):
        raise ConfigError("Environment placeholders cannot inject container values")

    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    if _INTEGER.fullmatch(value):
        return int(value)
    if _FLOAT.fullmatch(value):
        return float(value)
    return value


def _resolve_string(value: str) -> str | bool | int | float:
    if full_match := _FULL_PLACEHOLDER.fullmatch(value):
        resolved = _lookup_environment(
            full_match.group("name"), full_match.group("default")
        )
        return _convert_scalar(resolved)

    def replace(match: re.Match[str]) -> str:
        return _lookup_environment(match.group("name"), match.group("default"))

    resolved = _EMBEDDED_PLACEHOLDER.sub(replace, value)
    if "${" in resolved:
        raise ConfigError(
            "Configuration contains an unresolved environment placeholder"
        )
    return resolved


def _resolve_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_environment(item) for item in value]
    if isinstance(value, str):
        return _resolve_string(value)
    return value


def load_config_model(path: str | Path, model_type: type[_ModelT]) -> _ModelT:
    """Load one strict Pydantic model from duplicate-safe, env-aware YAML."""

    config_path = Path(path)
    try:
        content = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigError(f"Unable to read configuration file: {config_path}") from None

    try:
        document = yaml.load(content, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        message = (
            "Configuration contains duplicate YAML keys"
            if "duplicate" in str(exc)
            else "Invalid YAML configuration"
        )
        raise ConfigError(message) from None

    if not isinstance(document, Mapping):
        raise ConfigError("Configuration document must be a mapping")

    try:
        resolved = _resolve_environment(dict(document))
        return model_type.model_validate(resolved)
    except ConfigError:
        raise
    except ValidationError:
        raise ConfigError("Configuration validation failed") from None


def load_application_settings(path: str | Path) -> ApplicationSettings:
    """Load and validate application settings from a YAML file."""

    return load_config_model(path, ApplicationSettings)


def load_module_definitions(path: str | Path) -> ModulesSettings:
    """Load and validate dependency-manifest module definitions from YAML."""

    return load_config_model(path, ModulesSettings)
