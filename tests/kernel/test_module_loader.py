from __future__ import annotations

import itertools
import os
import subprocess
import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import pytest
from pydantic import ConfigDict, ValidationError

from nexus_kernel.config import ModulesSettings
from nexus_kernel.errors import ConfigError, ModuleDependencyError
from nexus_kernel.kernel import (
    HttpEndpointSpec,
    Module,
    ModuleConfig,
    ModuleContext,
    ModuleState,
    load_modules,
)


class GoodModuleConfig(ModuleConfig):
    limit: int = 4


class RequiredModuleConfig(ModuleConfig):
    label: str


class GoodModule(Module):
    name = "good"
    config_schema = GoodModuleConfig


class RequiredConfigModule(Module):
    name = "required_config"
    config_schema = RequiredModuleConfig


class NonStrictModuleConfig(ModuleConfig):
    model_config = ConfigDict(extra="allow", strict=False, frozen=False)


class NonStrictConfigModule(Module):
    name = "non_strict_config"
    config_schema = NonStrictModuleConfig


class OtherNameModule(Module):
    name = "other"


class BrokenConstructorModule(Module):
    name = "broken"

    def __init__(self) -> None:
        raise RuntimeError("constructor leaked secret-token")


class MissingApi(Protocol):
    def missing(self) -> str: ...


class MissingApiModule(Module):
    name = "missing_api"
    api_contract = MissingApi


_LOAD_EVENTS: list[str] = []


class AlphaModule(Module):
    name = "alpha"

    def __init__(self) -> None:
        super().__init__()
        _LOAD_EVENTS.append("construct:alpha")


class BetaModule(Module):
    name = "beta"
    dependencies = ("alpha",)

    def __init__(self) -> None:
        super().__init__()
        _LOAD_EVENTS.append("construct:beta")


class GammaModule(Module):
    name = "gamma"
    optional_dependencies = ("beta",)

    def __init__(self) -> None:
        super().__init__()
        _LOAD_EVENTS.append("construct:gamma")


class ReadsConfigDuringConstruction(Module):
    name = "reads_config"
    config_schema = GoodModuleConfig

    def __init__(self) -> None:
        super().__init__()
        self.observed_limit = self.config.limit


class MutatesDependenciesDuringConstruction(Module):
    name = "mutates_dependencies"

    def __init__(self) -> None:
        super().__init__()
        self.dependencies = ("unexpected",)


class MutatesProvidersDuringConstruction(Module):
    name = "mutates_providers"
    required_provider_names = ("database",)

    def __init__(self) -> None:
        super().__init__()
        self.required_provider_names = ()
        self.optional_provider_names = ("database",)


def _plugin_module() -> ModuleType:
    plugin = ModuleType("test_module_plugin")
    plugin.GoodModule = GoodModule
    plugin.RequiredConfigModule = RequiredConfigModule
    plugin.NonStrictConfigModule = NonStrictConfigModule
    plugin.OtherNameModule = OtherNameModule
    plugin.BrokenConstructorModule = BrokenConstructorModule
    plugin.MissingApiModule = MissingApiModule
    plugin.ReadsConfigDuringConstruction = ReadsConfigDuringConstruction
    plugin.MutatesDependenciesDuringConstruction = MutatesDependenciesDuringConstruction
    plugin.MutatesProvidersDuringConstruction = MutatesProvidersDuringConstruction
    plugin.NotAModule = object
    return plugin


def _dependency_plugin_modules() -> dict[str, ModuleType]:
    plugins: dict[str, ModuleType] = {}
    for name, module_type in (
        ("alpha", AlphaModule),
        ("beta", BetaModule),
        ("gamma", GammaModule),
    ):
        plugin = ModuleType(f"test_{name}_plugin")
        setattr(plugin, module_type.__name__, module_type)
        plugins[plugin.__name__] = plugin
    return plugins


def _settings(modules: dict[str, object]) -> ModulesSettings:
    return ModulesSettings.model_validate({"modules": modules})


def test_loader_skips_disabled_modules_and_preserves_original_index_for_diagnostics(
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
                "config": {"limit": 4},
            },
        }
    )

    loaded = load_modules(settings)

    assert [
        (item.module.name, item.required, item.original_index) for item in loaded
    ] == [("good", False, 1)]
    assert loaded[0].module.state is ModuleState.CREATED
    assert loaded[0].module.config.limit == 4
    assert isinstance(loaded[0].module.config, GoodModuleConfig)


def test_loader_uses_dependency_first_order_for_every_manifest_permutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = _dependency_plugin_modules()
    for name, plugin in plugins.items():
        monkeypatch.setitem(sys.modules, name, plugin)
    real_import_module = __import__("importlib").import_module

    definitions = {
        "alpha": {"class": "test_alpha_plugin.AlphaModule"},
        "beta": {
            "class": "test_beta_plugin.BetaModule",
            "dependencies": ["alpha"],
        },
        "gamma": {
            "class": "test_gamma_plugin.GammaModule",
            "optional_dependencies": ["beta"],
        },
    }

    for names in itertools.permutations(definitions):
        imported: list[str] = []
        _LOAD_EVENTS.clear()

        def recording_import(name: str, package: str | None = None) -> ModuleType:
            if name.startswith("test_"):
                imported.append(name)
            return real_import_module(name, package)

        monkeypatch.setattr(
            "nexus_kernel.kernel.loader.importlib.import_module", recording_import
        )
        settings = _settings({name: definitions[name] for name in names})

        loaded = load_modules(settings)

        assert [item.module.name for item in loaded] == ["alpha", "beta", "gamma"]
        assert imported == [
            "test_alpha_plugin",
            "test_beta_plugin",
            "test_gamma_plugin",
        ]
        assert _LOAD_EVENTS == [
            "construct:alpha",
            "construct:beta",
            "construct:gamma",
        ]


def test_loader_rejects_graph_errors_before_importing_any_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_import(name: str, package: str | None = None) -> ModuleType:
        raise AssertionError(f"implementation imported unexpectedly: {name}")

    monkeypatch.setattr(
        "nexus_kernel.kernel.loader.importlib.import_module", unexpected_import
    )

    with pytest.raises(ConfigError, match="missing or disabled"):
        load_modules(
            _settings(
                {
                    "consumer": {
                        "class": "invalid.consumer.Consumer",
                        "dependencies": ["dependency"],
                    },
                    "dependency": {
                        "class": "invalid.dependency.Dependency",
                        "enabled": False,
                    },
                }
            )
        )

    with pytest.raises(ConfigError, match=r"alpha -> beta -> alpha"):
        load_modules(
            _settings(
                {
                    "beta": {
                        "class": "invalid.beta.Beta",
                        "dependencies": ["alpha"],
                    },
                    "alpha": {
                        "class": "invalid.alpha.Alpha",
                        "dependencies": ["beta"],
                    },
                }
            )
        )


def test_loader_ignores_missing_or_disabled_optional_dependency_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = _dependency_plugin_modules()
    for name, plugin in plugins.items():
        monkeypatch.setitem(sys.modules, name, plugin)
    real_import_module = __import__("importlib").import_module
    imported: list[str] = []
    _LOAD_EVENTS.clear()

    def recording_import(name: str, package: str | None = None) -> ModuleType:
        if name.startswith("test_"):
            imported.append(name)
        return real_import_module(name, package)

    monkeypatch.setattr(
        "nexus_kernel.kernel.loader.importlib.import_module", recording_import
    )

    loaded = load_modules(
        _settings(
            {
                "gamma": {
                    "class": "test_gamma_plugin.GammaModule",
                    "optional_dependencies": ["beta"],
                },
                "beta": {
                    "class": "package_that_does_not_exist.BetaModule",
                    "enabled": False,
                },
            }
        )
    )

    assert [entry.module.name for entry in loaded] == ["gamma"]
    assert imported == ["test_gamma_plugin"]
    assert _LOAD_EVENTS == ["construct:gamma"]


def test_loader_does_not_import_consumer_when_dependency_config_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = _dependency_plugin_modules()
    for name, plugin in plugins.items():
        monkeypatch.setitem(sys.modules, name, plugin)
    real_import_module = __import__("importlib").import_module
    imported: list[str] = []
    _LOAD_EVENTS.clear()

    def recording_import(name: str, package: str | None = None) -> ModuleType:
        if name.startswith("test_"):
            imported.append(name)
        return real_import_module(name, package)

    monkeypatch.setattr(
        "nexus_kernel.kernel.loader.importlib.import_module", recording_import
    )

    with pytest.raises(ConfigError, match="alpha"):
        load_modules(
            _settings(
                {
                    "beta": {
                        "class": "test_beta_plugin.BetaModule",
                        "dependencies": ["alpha"],
                    },
                    "alpha": {
                        "class": "test_alpha_plugin.AlphaModule",
                        "config": {"not_allowed": True},
                    },
                }
            )
        )

    assert imported == ["test_alpha_plugin"]
    assert _LOAD_EVENTS == []


def test_loader_validates_every_config_before_constructing_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = _dependency_plugin_modules()
    for name, plugin in plugins.items():
        monkeypatch.setitem(sys.modules, name, plugin)
    _LOAD_EVENTS.clear()

    with pytest.raises(ConfigError, match="gamma"):
        load_modules(
            _settings(
                {
                    "alpha": {"class": "test_alpha_plugin.AlphaModule"},
                    "gamma": {
                        "class": "test_gamma_plugin.GammaModule",
                        "optional_dependencies": ["beta"],
                        "config": {"not_allowed": True},
                    },
                }
            )
        )

    assert _LOAD_EVENTS == []


def test_loader_rejects_manifest_drift_and_constructor_config_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "test_module_plugin", _plugin_module())

    with pytest.raises(ConfigError, match="mutates_dependencies"):
        load_modules(
            _settings(
                {
                    "mutates_dependencies": {
                        "class": (
                            "test_module_plugin.MutatesDependenciesDuringConstruction"
                        )
                    }
                }
            )
        )

    with pytest.raises(ConfigError, match="reads_config"):
        load_modules(
            _settings(
                {
                    "reads_config": {
                        "class": "test_module_plugin.ReadsConfigDuringConstruction",
                        "config": {"limit": 8},
                    }
                }
            )
        )

    with pytest.raises(ConfigError, match="mutates_providers"):
        load_modules(
            _settings(
                {
                    "mutates_providers": {
                        "class": "test_module_plugin.MutatesProvidersDuringConstruction"
                    }
                }
            )
        )


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("dependencies", ["dependency"]),
        ("dependencies", ("",)),
        ("dependencies", ("dependency", "dependency")),
        ("required_provider_names", ["database"]),
        ("required_provider_names", ("",)),
        ("required_provider_names", ("Database",)),
        ("required_provider_names", ("database-name",)),
        ("required_provider_names", ("database", "database")),
    ],
)
def test_loader_rejects_invalid_class_capability_declarations(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    value: object,
) -> None:
    invalid_type = type(
        "InvalidDeclarations",
        (Module,),
        {"name": "invalid_declarations", attribute: value},
    )
    plugin = ModuleType("invalid_declarations_plugin")
    plugin.InvalidDeclarations = invalid_type
    monkeypatch.setitem(sys.modules, plugin.__name__, plugin)

    with pytest.raises(ConfigError, match="invalid_declarations"):
        load_modules(
            _settings(
                {
                    "invalid_declarations": {
                        "class": ("invalid_declarations_plugin.InvalidDeclarations")
                    }
                }
            )
        )


def test_loader_rejects_required_optional_provider_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidProviders(Module):
        name = "invalid_providers"
        required_provider_names = ("database",)
        optional_provider_names = ("database",)

    plugin = ModuleType("invalid_providers_plugin")
    plugin.InvalidProviders = InvalidProviders
    monkeypatch.setitem(sys.modules, plugin.__name__, plugin)

    with pytest.raises(ConfigError, match="invalid_providers"):
        load_modules(
            _settings(
                {
                    "invalid_providers": {
                        "class": "invalid_providers_plugin.InvalidProviders"
                    }
                }
            )
        )


def test_module_direct_construction_lazily_binds_default_config_once() -> None:
    module = GoodModule()

    assert module.config.limit == 4
    with pytest.raises(RuntimeError, match="already bound"):
        module._set_config(GoodModuleConfig(limit=8))


@pytest.mark.parametrize(
    "graph",
    [
        {
            "alpha": (),
            "beta": ("alpha",),
            "gamma": ("beta",),
        },
        {
            "alpha": (),
            "beta": ("alpha",),
            "gamma": ("alpha",),
            "omega": ("beta", "gamma"),
        },
    ],
    ids=("three-module-chain", "four-module-diamond"),
)
def test_real_plugin_lifecycle_is_dependency_first_for_every_manifest_permutation(
    tmp_path: Path,
    graph: dict[str, tuple[str, ...]],
) -> None:
    expected_order = tuple(graph)
    for name, dependencies in graph.items():
        (tmp_path / f"plugin_{name}.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "from nexus_kernel.kernel import Module, ModuleContext\n"
            "def record(event):\n"
            "    with Path(os.environ['MODULE_EVENTS']).open('a', encoding='utf-8') as stream:\n"
            "        stream.write(event + '\\n')\n"
            f"record('import:{name}')\n"
            "class PluginModule(Module):\n"
            f"    name = {name!r}\n"
            f"    dependencies = {dependencies!r}\n"
            "    def __init__(self):\n"
            "        super().__init__()\n"
            f"        record('construct:{name}')\n"
            "    def on_register(self, context: ModuleContext):\n"
            f"        record('register:{name}')\n"
            "    async def on_start(self, context: ModuleContext):\n"
            f"        record('start:{name}')\n"
            "    async def on_stop(self, context: ModuleContext):\n"
            f"        record('stop:{name}')\n",
            encoding="utf-8",
        )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(tmp_path), str(Path(__file__).resolve().parents[2]))
    )
    expected_events = [
        *(f"import:{name}" for name in expected_order),
        *(f"construct:{name}" for name in expected_order),
        *(f"register:{name}" for name in expected_order),
        *(f"start:{name}" for name in expected_order),
        *(f"stop:{name}" for name in reversed(expected_order)),
    ]

    for index, permutation in enumerate(itertools.permutations(graph)):
        event_path = tmp_path / f"events-{index}.txt"
        environment["MODULE_EVENTS"] = str(event_path)
        definitions = {
            name: {
                "class": f"plugin_{name}.PluginModule",
                "dependencies": list(graph[name]),
            }
            for name in permutation
        }
        script = (
            "import asyncio\n"
            "from nexus_kernel.config import ModulesSettings\n"
            "from nexus_kernel.kernel import Kernel, load_modules\n"
            f"definitions = {definitions!r}\n"
            f"expected_order = {expected_order!r}\n"
            "async def main():\n"
            "    loaded = load_modules(ModulesSettings.model_validate("
            "{'modules': definitions}))\n"
            "    kernel = Kernel(loaded)\n"
            "    assert kernel.module_order == expected_order\n"
            "    await kernel.start()\n"
            "    assert await kernel.stop() == []\n"
            "asyncio.run(main())\n"
        )

        subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

        assert event_path.read_text(encoding="utf-8").splitlines() == expected_events


def test_module_config_is_strict_frozen_and_rejects_unknown_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "test_module_plugin", _plugin_module())
    module = load_modules(
        _settings(
            {
                "good": {
                    "class": "test_module_plugin.GoodModule",
                    "config": {"limit": 7},
                }
            }
        )
    )[0].module

    assert module.config.limit == 7
    with pytest.raises(ValidationError):
        module.config.limit = 8

    with pytest.raises(ConfigError):
        load_modules(
            _settings(
                {
                    "good": {
                        "class": "test_module_plugin.GoodModule",
                        "config": {"limit": "7"},
                    }
                }
            )
        )

    with pytest.raises(ConfigError):
        load_modules(
            _settings(
                {
                    "good": {
                        "class": "test_module_plugin.GoodModule",
                        "config": {"unknown": True},
                    }
                }
            )
        )


def test_loader_validates_required_config_before_constructing_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "test_module_plugin", _plugin_module())

    module = load_modules(
        _settings(
            {
                "required_config": {
                    "class": "test_module_plugin.RequiredConfigModule",
                    "config": {"label": "configured"},
                }
            }
        )
    )[0].module

    assert isinstance(module.config, RequiredModuleConfig)
    assert module.config.label == "configured"


def test_loader_rejects_module_config_schema_that_weakens_base_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "test_module_plugin", _plugin_module())

    with pytest.raises(ConfigError, match="non_strict_config"):
        load_modules(
            _settings(
                {
                    "non_strict_config": {
                        "class": "test_module_plugin.NonStrictConfigModule"
                    }
                }
            )
        )


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
    definition["config"] = {"password": "do-not-leak"}  # security-test-fixture

    with pytest.raises(ConfigError) as captured:
        load_modules(_settings({module_name: definition}))

    rendered = "".join(traceback.format_exception(captured.value))
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True
    assert "do-not-leak" not in str(captured.value)
    assert "secret-token" not in rendered


def test_loader_rejects_missing_protocol_implementation_as_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "test_module_plugin", _plugin_module())

    with pytest.raises(ConfigError) as captured:
        load_modules(
            _settings({"missing_api": {"class": "test_module_plugin.MissingApiModule"}})
        )

    assert not isinstance(captured.value, AttributeError)
    assert "has no attribute" not in str(captured.value)


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


def test_module_contract_walks_protocol_mro() -> None:
    module = ContractModule()

    assert module.get_api_contract() == ["base", "shared", "child"]
    assert not hasattr(module, "bind_kernel")
    assert not hasattr(module, "get_dependency")
    assert not hasattr(module, "get_provider")


def test_module_context_enforces_declared_capabilities() -> None:
    requested: list[tuple[str, str]] = []
    resources = {
        ("module", "required-module"): object(),
        ("module", "optional-module"): object(),
        ("provider", "database"): object(),
        ("provider", "cache"): object(),
    }
    context = ModuleContext._create(
        module_name="contract",
        required_modules=("required-module",),
        optional_modules=("optional-module",),
        required_providers=("database",),
        optional_providers=("cache",),
        module_resolver=lambda name: (
            requested.append(("module", name)) or resources[("module", name)]
        ),
        optional_module_resolver=lambda name: (
            requested.append(("module", name)) or resources.get(("module", name))
        ),
        provider_resolver=lambda name: (
            requested.append(("provider", name)) or resources[("provider", name)]
        ),
        optional_provider_resolver=lambda name: (
            requested.append(("provider", name)) or resources.get(("provider", name))
        ),
    )

    assert (
        context.require_module("required-module")
        is resources[("module", "required-module")]
    )
    assert (
        context.optional_module("optional-module")
        is resources[("module", "optional-module")]
    )
    assert context.require_provider("database") is resources[("provider", "database")]
    assert context.optional_provider("cache") is resources[("provider", "cache")]
    with pytest.raises(ModuleDependencyError, match="not declared"):
        context.require_module("optional-module")
    with pytest.raises(ModuleDependencyError, match="not declared"):
        context.optional_provider("database")
    assert not hasattr(context, "kernel")


class ExactApi(Protocol):
    def execute(self, value: str, *, retries: int = 1) -> str: ...


class WrongParameterModule(Module):
    name = "wrong_parameter"
    api_contract = ExactApi

    def execute(self, other: str, *, retries: int = 1) -> str:
        return other


class WrongDefaultModule(Module):
    name = "wrong_default"
    api_contract = ExactApi

    def execute(self, value: str, *, retries: int) -> str:
        return value


class WrongAsyncApi(Protocol):
    async def execute(self) -> str: ...


class WrongAsyncModule(Module):
    name = "wrong_async"
    api_contract = WrongAsyncApi

    def execute(self) -> str:
        return "wrong"


@pytest.mark.parametrize(
    "module",
    [WrongParameterModule(), WrongDefaultModule(), WrongAsyncModule()],
)
def test_module_protocol_validates_signature_and_async_shape(module: Module) -> None:
    with pytest.raises(ModuleDependencyError, match="API implementation is invalid"):
        module.validate_api_implementation()


class InstanceDescriptorApi(Protocol):
    def execute(self, value: str) -> str: ...


class StaticDescriptorMismatchModule(Module):
    name = "static_descriptor_mismatch"
    api_contract = InstanceDescriptorApi

    @staticmethod
    def execute(self, value: str) -> str:
        return value


class ClassDescriptorMismatchModule(Module):
    name = "class_descriptor_mismatch"
    api_contract = InstanceDescriptorApi

    @classmethod
    def execute(cls, self, value: str) -> str:
        return value


class StaticDescriptorApi(Protocol):
    @staticmethod
    def execute(value: str) -> str: ...


class InstanceForStaticDescriptorModule(Module):
    name = "instance_for_static_descriptor"
    api_contract = StaticDescriptorApi

    def execute(self, value: str) -> str:
        return value


class ClassForStaticDescriptorModule(Module):
    name = "class_for_static_descriptor"
    api_contract = StaticDescriptorApi

    @classmethod
    def execute(cls, value: str) -> str:
        return value


class ClassDescriptorApi(Protocol):
    @classmethod
    def execute(cls, value: str) -> str: ...


class InstanceForClassDescriptorModule(Module):
    name = "instance_for_class_descriptor"
    api_contract = ClassDescriptorApi

    def execute(self, value: str) -> str:
        return value


class StaticForClassDescriptorModule(Module):
    name = "static_for_class_descriptor"
    api_contract = ClassDescriptorApi

    @staticmethod
    def execute(cls, value: str) -> str:
        return value


@pytest.mark.parametrize(
    "module",
    [
        StaticDescriptorMismatchModule(),
        ClassDescriptorMismatchModule(),
        InstanceForStaticDescriptorModule(),
        ClassForStaticDescriptorModule(),
        InstanceForClassDescriptorModule(),
        StaticForClassDescriptorModule(),
    ],
)
def test_module_protocol_rejects_descriptor_binding_mismatches(module: Module) -> None:
    with pytest.raises(ModuleDependencyError, match="API implementation is invalid"):
        module.validate_api_implementation()


class MatchingDescriptorApi(Protocol):
    @staticmethod
    def static_value(value: str) -> str: ...

    @classmethod
    def class_value(cls, value: str) -> str: ...


class MatchingDescriptorModule(Module):
    name = "matching_descriptors"
    api_contract = MatchingDescriptorApi

    @staticmethod
    def static_value(value: str) -> str:
        return value

    @classmethod
    def class_value(cls, value: str) -> str:
        return f"{cls.name}:{value}"


class MatchingInstanceDescriptorModule(Module):
    name = "matching_instance_descriptor"
    api_contract = InstanceDescriptorApi

    def execute(self, value: str) -> str:
        return value


def test_module_protocol_accepts_matching_static_and_class_descriptors() -> None:
    module = MatchingDescriptorModule()
    instance_module = MatchingInstanceDescriptorModule()

    module.validate_api_implementation()
    instance_module.validate_api_implementation()

    assert module.get_api_contract() == ["static_value", "class_value"]


class AsyncStaticDescriptorApi(Protocol):
    @staticmethod
    async def execute(value: str) -> str: ...


class SyncStaticForAsyncDescriptorModule(Module):
    name = "sync_static_for_async_descriptor"
    api_contract = AsyncStaticDescriptorApi

    @staticmethod
    def execute(value: str) -> str:
        return value


class SyncStaticDescriptorApi(Protocol):
    @staticmethod
    def execute(value: str) -> str: ...


class AsyncStaticForSyncDescriptorModule(Module):
    name = "async_static_for_sync_descriptor"
    api_contract = SyncStaticDescriptorApi

    @staticmethod
    async def execute(value: str) -> str:
        return value


class AsyncClassDescriptorApi(Protocol):
    @classmethod
    async def execute(cls, value: str) -> str: ...


class SyncClassForAsyncDescriptorModule(Module):
    name = "sync_class_for_async_descriptor"
    api_contract = AsyncClassDescriptorApi

    @classmethod
    def execute(cls, value: str) -> str:
        return value


class SyncClassDescriptorApi(Protocol):
    @classmethod
    def execute(cls, value: str) -> str: ...


class AsyncClassForSyncDescriptorModule(Module):
    name = "async_class_for_sync_descriptor"
    api_contract = SyncClassDescriptorApi

    @classmethod
    async def execute(cls, value: str) -> str:
        return value


@pytest.mark.parametrize(
    "module",
    [
        SyncStaticForAsyncDescriptorModule(),
        AsyncStaticForSyncDescriptorModule(),
        SyncClassForAsyncDescriptorModule(),
        AsyncClassForSyncDescriptorModule(),
    ],
)
def test_descriptor_protocol_rejects_static_and_class_async_shape_mismatches(
    module: Module,
) -> None:
    with pytest.raises(ModuleDependencyError, match="API implementation is invalid"):
        module.validate_api_implementation()


class MatchingAsyncDescriptorApi(Protocol):
    @staticmethod
    async def static_value(value: str) -> str: ...

    @classmethod
    async def class_value(cls, value: str) -> str: ...


class MatchingAsyncDescriptorModule(Module):
    name = "matching_async_descriptors"
    api_contract = MatchingAsyncDescriptorApi

    @staticmethod
    async def static_value(value: str) -> str:
        return value

    @classmethod
    async def class_value(cls, value: str) -> str:
        return f"{cls.name}:{value}"


@pytest.mark.asyncio
async def test_descriptor_protocol_accepts_matching_async_static_and_class_methods() -> (
    None
):
    module = MatchingAsyncDescriptorModule()

    module.validate_api_implementation()

    assert await module.static_value("static") == "static"
    assert await module.class_value("class") == "matching_async_descriptors:class"


@pytest.mark.asyncio
async def test_module_default_lifecycle_hooks_are_no_ops() -> None:
    module = Module()

    assert module.on_register(Any) is None
    assert await module.on_start(Any) is None
    assert await module.on_stop(Any) is None
    assert module.get_worker_specs() == []


def test_module_info_contains_only_public_metadata() -> None:
    module = ContractModule()

    info = module.get_info()

    assert info["name"] == "contract"
    assert info["state"] == "created"
    assert info["required_provider_names"] == ["database"]
    assert "never-expose" not in repr(info)
    assert "config" not in info


def test_module_states_define_the_public_lifecycle() -> None:
    assert [state.value for state in ModuleState] == [
        "created",
        "registered",
        "starting",
        "ready",
        "degraded",
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
        {
            "path": "/items",
            "methods": ("GET",),
            "target_api": "list_items",
            "timeout_seconds": True,
        },
        {
            "path": "/items",
            "methods": ("GET",),
            "target_api": "list_items",
            "timeout_seconds": float("nan"),
        },
        {
            "path": "/items",
            "methods": ("GET",),
            "target_api": "list_items",
            "timeout_seconds": float("inf"),
        },
        {
            "path": "/items",
            "methods": ("GET",),
            "target_api": "list_items",
            "timeout_seconds": -float("inf"),
        },
    ],
)
def test_http_endpoint_spec_rejects_invalid_contracts(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        HttpEndpointSpec(**arguments)  # type: ignore[arg-type]
