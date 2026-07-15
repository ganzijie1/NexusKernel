from __future__ import annotations

import sys
from types import ModuleType
from typing import Protocol

import pytest

from nexuskernel.config import ModulesSettings
from nexuskernel.errors import ConfigError
from nexuskernel.kernel import (
    HttpEndpointSpec,
    Module,
    ModuleState,
    ProviderState,
    load_modules,
)


class GoodModule(Module):
    name = "good"


class OtherNameModule(Module):
    name = "other"


class BrokenConstructorModule(Module):
    name = "broken"

    def __init__(self) -> None:
        raise RuntimeError("constructor leaked secret-token")


def _plugin_module() -> ModuleType:
    plugin = ModuleType("test_module_plugin")
    plugin.GoodModule = GoodModule
    plugin.OtherNameModule = OtherNameModule
    plugin.BrokenConstructorModule = BrokenConstructorModule
    plugin.NotAModule = object
    return plugin


def _settings(modules: dict[str, object]) -> ModulesSettings:
    return ModulesSettings.model_validate({"modules": modules})


def test_loader_skips_disabled_modules_and_preserves_declaration_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "test_module_plugin", _plugin_module())
    settings = _settings(
        {
            "disabled": {
                "class": "not.imported.Disabled",
                "enabled": False,
            },
            "good": {
                "class": "test_module_plugin.GoodModule",
                "required": False,
                "config": {"nested": {"secret": "redact-me"}, "limit": 4},
            },
        }
    )

    loaded = load_modules(settings)

    assert [
        (item.module.name, item.required, item.original_index) for item in loaded
    ] == [("good", False, 1)]
    assert loaded[0].module.state is ModuleState.CREATED
    assert loaded[0].module.config.get("limit") == 4
    assert loaded[0].module.config.get("nested.secret") == "redact-me"


def test_bound_module_config_is_read_only_and_returns_defensive_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "test_module_plugin", _plugin_module())
    source = {"nested": {"value": 1}}
    module = load_modules(
        _settings(
            {
                "good": {
                    "class": "test_module_plugin.GoodModule",
                    "config": source,
                }
            }
        )
    )[0].module

    source["nested"]["value"] = 2
    exported = module.config.to_dict()
    exported["nested"]["value"] = 3

    assert module.config.get("nested.value") == 1
    assert not hasattr(module.config, "set")


@pytest.mark.parametrize(
    ("module_name", "definition"),
    [
        ("missing", {"class": "package_does_not_exist.Missing"}),
        ("invalid", {"class": "test_module_plugin.NotAModule"}),
        ("broken", {"class": "test_module_plugin.BrokenConstructorModule"}),
        ("good", {"class": "test_module_plugin.OtherNameModule"}),
    ],
    ids=("invalid-import", "not-module", "constructor", "name-mismatch"),
)
def test_loader_normalizes_module_failures_without_leaking_config(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    definition: dict[str, object],
) -> None:
    monkeypatch.setitem(sys.modules, "test_module_plugin", _plugin_module())
    definition["config"] = {"password": "do-not-leak"}

    with pytest.raises(ConfigError) as captured:
        load_modules(_settings({module_name: definition}))

    assert captured.value.__cause__ is not None
    assert "do-not-leak" not in str(captured.value)
    assert "secret-token" not in str(captured.value)


class BaseApi(Protocol):
    def base(self) -> str: ...

    def shared(self) -> str: ...

    def _private(self) -> str: ...


class ChildApi(BaseApi, Protocol):
    def shared(self) -> str: ...

    def child(self) -> str: ...


class ContractModule(Module):
    name = "contract"
    version = "2.0.0"
    dependencies = ("required-module",)
    optional_dependencies = ("optional-module",)
    api_contract = ChildApi
    required_provider_names = ("database",)
    optional_provider_names = ("cache",)


def test_module_contract_walks_protocol_mro_and_delegates_dependency() -> None:
    dependency = object()

    class KernelProbe:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def get_module(self, name: str) -> object:
            self.requested.append(name)
            return dependency

    kernel = KernelProbe()
    module = ContractModule()
    module.bind_kernel(kernel)

    assert module.get_api_contract() == ["base", "shared", "child"]
    assert module.get_dependency("required-module") is dependency
    assert kernel.requested == ["required-module"]


@pytest.mark.asyncio
async def test_module_default_lifecycle_hooks_are_no_ops() -> None:
    module = Module()

    assert module.on_register(object()) is None
    assert await module.on_start(object()) is None
    assert await module.on_stop(object()) is None


def test_module_info_contains_only_public_metadata() -> None:
    module = ContractModule()
    module.bind_config({"password": "never-expose"})

    info = module.get_info()

    assert info["name"] == "contract"
    assert info["state"] == "created"
    assert info["required_provider_names"] == ["database"]
    assert "never-expose" not in repr(info)
    assert "config" not in info


def test_module_and_provider_states_define_the_public_lifecycle() -> None:
    assert [state.value for state in ModuleState] == [
        "created",
        "registered",
        "starting",
        "ready",
        "degraded",
        "stopped",
        "failed",
    ]
    assert [state.value for state in ProviderState] == [
        "registered",
        "starting",
        "ready",
        "degraded",
        "disabled",
        "stopped",
        "failed",
    ]


def test_http_endpoint_spec_normalizes_valid_methods() -> None:
    endpoint = HttpEndpointSpec(
        path="/items",
        methods=("GET", "POST"),
        target_api="list_items",
        timeout_seconds=1.5,
    )

    assert endpoint.methods == ("GET", "POST")
    assert endpoint.status_code == 200
    assert endpoint.auth_required is False


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "items", "methods": ("GET",), "target_api": "list_items"},
        {"path": "/items", "methods": (), "target_api": "list_items"},
        {"path": "/items", "methods": ("get",), "target_api": "list_items"},
        {"path": "/items", "methods": ("GET",), "target_api": "  "},
        {
            "path": "/items",
            "methods": ("GET",),
            "target_api": "list_items",
            "timeout_seconds": 0,
        },
    ],
)
def test_http_endpoint_spec_rejects_invalid_contracts(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        HttpEndpointSpec(**arguments)  # type: ignore[arg-type]
