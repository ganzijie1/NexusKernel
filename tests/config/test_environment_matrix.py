from __future__ import annotations

import re
from pathlib import Path

from tests.project_contract import load_project_contract


ROOT = Path(__file__).resolve().parents[2]
PROJECT = load_project_contract(ROOT)
PLACEHOLDER = re.compile(
    r"\$\{(?P<name>[A-Z][A-Z0-9_]*)(?::(?P<operator>[-?])(?P<default>[^}]*))?}"
)
PROFILE_KEY_SUFFIXES = {
    "dev": {"RUNTIME_EXTRAS"},
    "preprod": {"IMAGE"},
    "prod": {"IMAGE"},
}
GLOBAL_PROFILE_KEYS = {
    "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS",
    "LITELLM_LOCAL_MODEL_COST_MAP",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
}
SENSITIVE_SUFFIXES = {
    "SERVICE_BEARER_TOKEN",
    "AI_API_KEY",
    "AI_BASE_URL",
    "MONGO_DSN",
    "MONGO_DATABASE",
    "REDIS_DSN",
    "POSTGRES_DSN",
    "QDRANT_URL",
    "QDRANT_API_KEY",
}


def _runtime_keys() -> set[str]:
    return set(_runtime_defaults())


def _runtime_defaults() -> dict[str, str]:
    defaults: dict[str, str] = {}
    for path in sorted((ROOT / "config").glob("*.yaml")):
        for match in PLACEHOLDER.finditer(path.read_text(encoding="utf-8")):
            name = match.group("name")
            default = match.group("default")
            assert match.group("operator") == "-", (
                f"runtime placeholder has no default: {name}"
            )
            assert default is not None
            previous = defaults.setdefault(name, default)
            assert previous == default, f"runtime default is inconsistent: {name}"
    return defaults


def _profile_values(profile: str) -> dict[str, str]:
    values = {
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
        "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
    }
    if profile == "dev":
        values.update(
            {
                PROJECT.environment_name("RUNTIME_EXTRAS"): "ai,mongo,redis",
                PROJECT.environment_name("LOG_LEVEL"): "DEBUG",
            }
        )
    else:
        values.update(
            {
                PROJECT.environment_name("IMAGE"): "",
                PROJECT.environment_name("LOG_SERIALIZE"): "true",
            }
        )
    return values


def _environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid environment example line: {path.name}"
        assert key not in values, f"duplicate environment example key: {key}"
        values[key] = value
    return values


def _documented_module_defaults() -> dict[str, str]:
    values: dict[str, str] = {}
    guidance = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    for line in guidance.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            continue
        environment_name = cells[1].strip("`")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", environment_name) is None:
            continue
        documented_default = cells[2].strip("`")
        values[environment_name] = (
            "" if documented_default == "空" else documented_default
        )
    return values


def test_environment_examples_cover_exact_runtime_consumer_matrix() -> None:
    runtime_defaults = _runtime_defaults()

    sensitive_keys = {PROJECT.environment_name(key) for key in SENSITIVE_SUFFIXES}
    for profile, suffixes in PROFILE_KEY_SUFFIXES.items():
        profile_keys = {
            *(PROJECT.environment_name(key) for key in suffixes),
            *GLOBAL_PROFILE_KEYS,
        }
        values = _environment(ROOT / "docker" / f".env.{profile}.example")
        expected = {**runtime_defaults, **_profile_values(profile)}
        assert set(expected) == set(runtime_defaults) | profile_keys
        assert values == expected
        assert all(values[key] == "" for key in sensitive_keys)


def test_compose_docker_ci_and_docs_share_the_environment_contract() -> None:
    for profile, suffixes in PROFILE_KEY_SUFFIXES.items():
        profile_keys = {PROJECT.environment_name(key) for key in suffixes}
        compose = (ROOT / "docker" / f"docker-compose-{profile}.yml").read_text(
            encoding="utf-8"
        )
        expected = {PROJECT.environment_name("PORT")} | profile_keys
        assert {match.group("name") for match in PLACEHOLDER.finditer(compose)} == (
            expected
        )

    dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    runtime_extras = PROJECT.environment_name("RUNTIME_EXTRAS")
    assert dockerfile.startswith(f"ARG {runtime_extras}=ai\n")
    assert runtime_extras in dockerfile

    pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    for profile in PROFILE_KEY_SUFFIXES:
        assert f"--env-file docker/.env.{profile}.example" in pipeline

    runtime_defaults = _runtime_defaults()
    module_defaults = {
        key: value
        for key, value in runtime_defaults.items()
        if key.startswith(f"{PROJECT.env_prefix}_AI_")
        or key.startswith(f"{PROJECT.env_prefix}_ADK_")
        or key.startswith(f"{PROJECT.env_prefix}_AGENT_RUNS_")
    }
    service_token = PROJECT.environment_name("SERVICE_BEARER_TOKEN")
    module_defaults[service_token] = runtime_defaults[service_token]
    assert _documented_module_defaults() == module_defaults

    guidance = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    assert "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS" in guidance
    assert "LITELLM_LOCAL_MODEL_COST_MAP" in guidance
    assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in guidance
    assert PROJECT.environment_name("IMAGE") in guidance

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--env-file docker/.env.dev" in readme
