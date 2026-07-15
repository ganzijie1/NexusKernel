from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nexuskernel.config import (
    ApplicationSettings,
    load_application_settings,
    load_module_definitions,
)
from nexuskernel.errors import ConfigError


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_application_settings_defaults_and_forbids_unknown_fields() -> None:
    settings = ApplicationSettings()

    assert settings.service.name == "NexusKernel Starter"
    assert settings.server.port == 8030
    assert settings.runtime.role == "api"

    with pytest.raises(ValidationError):
        ApplicationSettings.model_validate({"server": {"unexpected": True}})


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("server", "max_request_body_bytes", 0),
        ("server", "request_timeout_seconds", 0),
        ("monitoring", "metrics_cache_ttl_seconds", 0),
        ("monitoring", "health_timeout_seconds", -1),
    ],
)
def test_application_settings_requires_positive_runtime_limits(
    section: str, field: str, value: int
) -> None:
    with pytest.raises(ValidationError):
        ApplicationSettings.model_validate({section: {field: value}})


def test_application_settings_restricts_runtime_role() -> None:
    with pytest.raises(ValidationError):
        ApplicationSettings.model_validate({"runtime": {"role": "scheduler"}})


def test_load_application_settings_resolves_recursive_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_VERSION", "2.4.1")
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("SERIALIZE", "false")
    monkeypatch.setenv("TTL", "2.5")
    path = _write(
        tmp_path / "settings.yaml",
        """service:
  version: ${SERVICE_VERSION}
server:
  host: ${HOST:-127.0.0.1}
  port: ${PORT}
runtime:
  role: api
monitoring:
  metrics_cache_ttl_seconds: ${TTL}
logging:
  serialize: ${SERIALIZE}
cors:
  allow_origins:
    - https://${PUBLIC_HOST:-example.test}
""",
    )

    settings = load_application_settings(path)

    assert settings.service.version == "2.4.1"
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 9123
    assert settings.monitoring.metrics_cache_ttl_seconds == 2.5
    assert settings.logging.serialize is False
    assert settings.cors.allow_origins == ["https://example.test"]


def test_load_application_settings_rejects_missing_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_HOST", raising=False)
    path = _write(tmp_path / "settings.yaml", "server:\n  host: ${MISSING_HOST}\n")

    with pytest.raises(ConfigError, match="MISSING_HOST"):
        load_application_settings(path)


@pytest.mark.parametrize("value", ["{secret: value}", "[secret, value]"])
def test_load_application_settings_rejects_container_environment_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("INJECTED", value)
    path = _write(
        tmp_path / "settings.yaml",
        "cors:\n  allow_origins: ${INJECTED}\n",
    )

    with pytest.raises(ConfigError, match="container"):
        load_application_settings(path)


def test_load_module_definitions_preserves_order_alias_and_typed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FEATURE_ENABLED", "true")
    path = _write(
        tmp_path / "modules.yaml",
        """modules:
  alpha:
    class: tests.fixtures.AlphaModule
    config:
      enabled: ${FEATURE_ENABLED}
  beta:
    class: tests.fixtures.BetaModule
    enabled: false
    required: false
""",
    )

    settings = load_module_definitions(path)

    assert list(settings.modules) == ["alpha", "beta"]
    assert settings.modules["alpha"].class_path == "tests.fixtures.AlphaModule"
    assert settings.modules["alpha"].config == {"enabled": True}
    assert settings.modules["beta"].enabled is False
    assert settings.modules["beta"].required is False


@pytest.mark.parametrize(
    "content",
    [
        "server:\n  port: 8030\n  port: 9123\n",
        "modules:\n  alpha:\n    class: one.Alpha\n  alpha:\n    class: two.Alpha\n",
    ],
)
def test_yaml_loader_rejects_duplicate_keys(tmp_path: Path, content: str) -> None:
    path = _write(tmp_path / "duplicate.yaml", content)
    loader = (
        load_module_definitions
        if content.startswith("modules:")
        else load_application_settings
    )

    with pytest.raises(ConfigError, match="duplicate"):
        loader(path)


@pytest.mark.parametrize(
    "content",
    [
        "- not\n- a\n- mapping\n",
        "server: [unterminated\n",
        "server:\n  host: ${STILL_UNRESOLVED\n",
    ],
)
def test_yaml_loader_normalizes_invalid_documents_to_config_error(
    tmp_path: Path, content: str
) -> None:
    path = _write(tmp_path / "invalid.yaml", content)

    with pytest.raises(ConfigError):
        load_application_settings(path)


def test_yaml_loader_normalizes_missing_file_to_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_application_settings(tmp_path / "missing.yaml")


@pytest.mark.parametrize("name", ["", "   "])
def test_module_names_must_be_non_empty(tmp_path: Path, name: str) -> None:
    path = _write(
        tmp_path / "modules.yaml",
        f'modules:\n  "{name}":\n    class: sample.Module\n',
    )

    with pytest.raises(ConfigError):
        load_module_definitions(path)
