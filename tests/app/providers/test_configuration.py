from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from nexus_kernel.errors import ConfigError
from nexus_kernel.providers import ProviderRegistry
from tests.project_contract import load_project_contract


PROJECT = load_project_contract()


def _providers_module() -> Any:
    return importlib.import_module("app.providers")


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_application_provider_package_exists() -> None:
    assert importlib.util.find_spec("app.providers") is not None


def test_provider_loader_does_not_depend_on_private_framework_config_api() -> None:
    source = Path("app/providers/config.py").read_text(encoding="utf-8")

    assert "_load_model" not in source


def test_empty_repository_configuration_disables_fixed_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("MONGO_DSN", "MONGO_DATABASE", "REDIS_DSN", "POSTGRES_DSN"):
        monkeypatch.delenv(PROJECT.environment_name(name), raising=False)
    monkeypatch.delenv(PROJECT.environment_name("QDRANT_URL"), raising=False)
    monkeypatch.delenv(PROJECT.environment_name("QDRANT_API_KEY"), raising=False)
    module = _providers_module()

    settings = module.load_provider_settings(Path("config/providers.yaml"))
    registry = ProviderRegistry()
    module.register_infrastructure_providers(registry, settings)

    assert [item["name"] for item in registry.statuses()] == [
        "mongo",
        "redis",
        "postgres",
        "qdrant",
    ]
    assert {item["state"] for item in registry.statuses()} == {"disabled"}


@pytest.mark.parametrize(
    "content",
    [
        "providers:\n  mongo:\n    dsn: mongodb://first.invalid/db\n    dsn: mongodb://second.invalid/db\n",
        "providers:\n  custom:\n    class: package.CustomProvider\n",
        "providers:\n  redis:\n    dsn: ftp://cache.invalid/value-that-must-not-leak\n",
        "providers:\n  postgres:\n    dsn: ${MISSING_PROVIDER_DSN}\n",
    ],
)
def test_provider_loader_fails_closed_without_leaking_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    monkeypatch.delenv("MISSING_PROVIDER_DSN", raising=False)
    module = _providers_module()
    path = _write(tmp_path / "providers.yaml", content)

    with pytest.raises(ConfigError) as captured:
        module.load_provider_settings(path)

    message = str(captured.value)
    assert "value-that-must-not-leak" not in message
    assert "mongodb://" not in message


def test_provider_secrets_are_masked_in_model_output() -> None:
    module = _providers_module()

    settings = module.InfrastructureProviderSettings.model_validate(
        {
            "providers": {
                "mongo": {
                    "dsn": "mongodb://mongo.invalid/app",
                    "database": "app",
                },
                "redis": {"dsn": "redis://cache.invalid/0"},
                "postgres": {"dsn": "postgresql://postgres.invalid/app"},
                "qdrant": {
                    "url": "https://qdrant.invalid",
                    "api_key": "opaque-qdrant-value",  # security-test-fixture
                },
            }
        }
    )

    rendered = repr(settings)
    assert "mongodb://" not in rendered
    assert "redis://" not in rendered
    assert "postgresql://" not in rendered
    assert "opaque-qdrant-value" not in rendered


@pytest.mark.parametrize(
    "provider_config",
    [
        {"mongo": {"required": "false"}},
        {"redis": {"timeout_seconds": "5"}},
        {"postgres": {"min_pool_size": "5"}},
        {"qdrant": {"required": "false"}},
    ],
)
def test_provider_models_reject_coerced_scalar_types(
    provider_config: dict[str, dict[str, str]],
) -> None:
    module = _providers_module()

    with pytest.raises(ValidationError):
        module.InfrastructureProviderSettings.model_validate(
            {"providers": provider_config}
        )


@pytest.mark.parametrize("provider", ["mongo", "redis", "postgres", "qdrant"])
def test_required_provider_must_have_enabling_configuration(provider: str) -> None:
    module = _providers_module()

    with pytest.raises(ValidationError, match="required provider must be configured"):
        module.InfrastructureProviderSettings.model_validate(
            {"providers": {provider: {"required": True}}}
        )


def test_required_unconfigured_provider_yaml_fails_closed(tmp_path: Path) -> None:
    module = _providers_module()
    path = _write(
        tmp_path / "providers.yaml",
        "providers:\n  mongo:\n    required: true\n",
    )

    with pytest.raises(ConfigError, match="validation failed"):
        module.load_provider_settings(path)


@pytest.mark.parametrize(
    "field",
    [
        'required: "false"',
        'timeout_seconds: "5"',
    ],
)
def test_provider_yaml_rejects_quoted_scalar_types(tmp_path: Path, field: str) -> None:
    module = _providers_module()
    path = _write(tmp_path / "providers.yaml", f"providers:\n  redis:\n    {field}\n")

    with pytest.raises(ConfigError, match="validation failed"):
        module.load_provider_settings(path)


class _FakeProvider:
    def __init__(self, kind: str, arguments: tuple[Any, ...], keywords: dict[str, Any]):
        self.kind = kind
        self.arguments = arguments
        self.keywords = keywords

    async def start(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def _fake_adapter(kind: str, calls: list[_FakeProvider]) -> type[Any]:
    class Adapter:
        @classmethod
        def from_uri(cls, *args: Any, **kwargs: Any) -> _FakeProvider:
            provider = _FakeProvider(kind, args, kwargs)
            calls.append(provider)
            return provider

        @classmethod
        def from_url(cls, *args: Any, **kwargs: Any) -> _FakeProvider:
            provider = _FakeProvider(kind, args, kwargs)
            calls.append(provider)
            return provider

        @classmethod
        def from_dsn(cls, *args: Any, **kwargs: Any) -> _FakeProvider:
            provider = _FakeProvider(kind, args, kwargs)
            calls.append(provider)
            return provider

    return Adapter


@pytest.mark.asyncio
async def test_explicit_factories_register_only_fixed_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _providers_module()
    factory_module = importlib.import_module("app.providers.factory")
    calls: list[_FakeProvider] = []
    monkeypatch.setattr(
        factory_module, "MongoDocumentStore", _fake_adapter("mongo", calls)
    )
    monkeypatch.setattr(
        factory_module, "RedisStreamQueue", _fake_adapter("redis", calls)
    )
    monkeypatch.setattr(
        factory_module, "PostgresDatabase", _fake_adapter("postgres", calls)
    )
    monkeypatch.setattr(
        factory_module, "QdrantVectorStore", _fake_adapter("qdrant", calls)
    )
    settings = module.InfrastructureProviderSettings.model_validate(
        {
            "providers": {
                "mongo": {
                    "dsn": "mongodb://mongo.invalid/app",
                    "database": "app",
                    "required": True,
                },
                "redis": {"dsn": "redis://cache.invalid/0"},
                "postgres": {"dsn": "postgresql://postgres.invalid/app"},
                "qdrant": {
                    "url": "https://qdrant.invalid",
                    "api_key": "opaque-value",  # security-test-fixture
                },
            }
        }
    )
    registry = ProviderRegistry()

    module.register_infrastructure_providers(registry, settings)
    await registry.initialize_all()

    assert [provider.kind for provider in calls] == [
        "mongo",
        "redis",
        "postgres",
        "qdrant",
    ]
    assert [item["state"] for item in registry.statuses()] == [
        "ready",
        "ready",
        "ready",
        "ready",
    ]
    assert registry.statuses()[0]["required"] is True
    assert "mongodb://mongo.invalid/app" in calls[0].arguments
    assert "opaque-value" in calls[3].keywords.values()
