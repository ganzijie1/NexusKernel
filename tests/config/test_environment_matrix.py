from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)")
PROFILE_KEYS = {
    "dev": {"NEXUS_KERNEL_RUNTIME_EXTRAS"},
    "preprod": {"NEXUS_KERNEL_IMAGE"},
    "prod": {"NEXUS_KERNEL_IMAGE"},
}
SENSITIVE_KEYS = {
    "NEXUS_KERNEL_SERVICE_BEARER_TOKEN",
    "NEXUS_KERNEL_AI_API_KEY",
    "NEXUS_KERNEL_AI_BASE_URL",
    "NEXUS_KERNEL_MONGO_DSN",
    "NEXUS_KERNEL_MONGO_DATABASE",
    "NEXUS_KERNEL_REDIS_DSN",
    "NEXUS_KERNEL_POSTGRES_DSN",
    "NEXUS_KERNEL_QDRANT_URL",
    "NEXUS_KERNEL_QDRANT_API_KEY",
}


def _runtime_keys() -> set[str]:
    keys: set[str] = set()
    for path in sorted((ROOT / "config").glob("*.yaml")):
        keys.update(PLACEHOLDER.findall(path.read_text(encoding="utf-8")))
    return keys


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


def test_environment_examples_cover_exact_runtime_consumer_matrix() -> None:
    runtime_keys = _runtime_keys()

    for profile, profile_keys in PROFILE_KEYS.items():
        values = _environment(ROOT / "docker" / f".env.{profile}.example")
        assert set(values) == runtime_keys | profile_keys
        assert all(values[key] == "" for key in SENSITIVE_KEYS)


def test_compose_docker_ci_and_docs_share_the_environment_contract() -> None:
    for profile, profile_keys in PROFILE_KEYS.items():
        compose = (ROOT / "docker" / f"docker-compose-{profile}.yml").read_text(
            encoding="utf-8"
        )
        expected = {"NEXUS_KERNEL_PORT"} | profile_keys
        assert set(PLACEHOLDER.findall(compose)) == expected

    dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("ARG NEXUS_KERNEL_RUNTIME_EXTRAS=ai\n")
    assert "NEXUS_KERNEL_RUNTIME_EXTRAS" in dockerfile

    pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    for profile in PROFILE_KEYS:
        assert f"--env-file docker/.env.{profile}.example" in pipeline

    guidance = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    ai_keys = {
        key
        for key in _runtime_keys()
        if key.startswith("NEXUS_KERNEL_AI_") or key.startswith("NEXUS_KERNEL_ADK_")
    }
    assert all(key in guidance for key in ai_keys)
    assert "NEXUS_KERNEL_IMAGE" in guidance

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--env-file docker/.env.dev" in readme
