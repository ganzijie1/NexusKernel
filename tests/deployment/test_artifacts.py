from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_EXTRAS = ("ai", "mongo", "redis", "qdrant", "otel", "postgres")


def _project_identity() -> tuple[str, str]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        distribution = tomllib.load(stream)["project"]["name"]
    application = (ROOT / "config/application.yaml").read_text(encoding="utf-8")
    match = re.search(r"\$\{([A-Z][A-Z0-9_]*)_HOST(?::-[^}]*)?}", application)
    assert isinstance(distribution, str)
    assert match is not None
    return distribution, match.group(1)


def test_dockerfile_is_multistage_non_root_and_role_driven() -> None:
    content = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    distribution, env_prefix = _project_identity()
    argument = f"{env_prefix}_RUNTIME_EXTRAS"

    assert content.count("FROM ") >= 2
    assert "groupadd --gid 10001 nexus_kernel" in content
    assert "useradd --uid 10001 --gid nexus_kernel" in content
    assert "USER 10001:10001" in content
    assert "COPY --chown=nexus_kernel:nexus_kernel config ./config" in content
    assert 'CMD ["python", "-m", "app.launcher"]' in content
    assert "COPY ." not in content
    assert "PIP_DEFAULT_TIMEOUT=120" in content
    assert content.startswith(f"ARG {argument}=ai\n")
    assert content.count(f"ARG {argument}") == 3
    builder, runtime = content.split("FROM ")[1:]
    assert f"ARG {argument}" in builder
    assert f'".[${{{argument}}}]"' in builder
    assert f"ARG {argument}" in runtime
    assert f'"{distribution}[${{{argument}}}]"' in runtime

    script_match = re.search(r"python -c \\\n\s+'([^']+)'", builder)
    assert script_match is not None
    script = script_match.group(1)
    assert "requested" in script and "requested.issubset(allowed)" in script
    for value, valid in (
        ("ai", True),
        (",".join(RUNTIME_EXTRAS), True),
        ("", False),
        ("ai,ai", False),
        ("ai,unknown", False),
    ):
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, argument: value},
            check=False,
        )
        assert (result.returncode == 0) is valid, value


def test_compose_files_are_parseable_and_have_no_external_or_fixed_networks() -> None:
    _, env_prefix = _project_identity()
    paths = sorted((ROOT / "docker").glob("docker-compose-*.yml"))
    assert [path.name for path in paths] == [
        "docker-compose-dev.yml",
        "docker-compose-preprod.yml",
        "docker-compose-prod.yml",
    ]

    for path in paths:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "api" in document["services"]
        assert (
            document["services"]["api"]["environment"][f"{env_prefix}_RUNTIME_ROLE"]
            == "api"
        )
        assert (
            document["services"]["worker"]["environment"][f"{env_prefix}_RUNTIME_ROLE"]
            == "worker"
        )
        serialized = path.read_text(encoding="utf-8")
        assert "ipv4_address" not in serialized
        assert "external:" not in serialized
        assert "password:" not in serialized.casefold()


def test_compose_profiles_enforce_runtime_dependency_contracts() -> None:
    _, env_prefix = _project_identity()
    dev = yaml.safe_load(
        (ROOT / "docker/docker-compose-dev.yml").read_text(encoding="utf-8")
    )
    assert dev["services"]["api"]["build"]["args"] == {
        f"{env_prefix}_RUNTIME_EXTRAS": (
            f"${{{env_prefix}_RUNTIME_EXTRAS:-ai,mongo,redis}}"
        )
    }

    for profile in ("preprod", "prod"):
        document = yaml.safe_load(
            (ROOT / f"docker/docker-compose-{profile}.yml").read_text(encoding="utf-8")
        )
        assert set(document["services"]) == {"api", "worker"}
        for role in ("api", "worker"):
            assert document["services"][role]["image"] == (
                f"${{{env_prefix}_IMAGE:?{env_prefix}_IMAGE must be set}}"
            )
        assert "volumes" not in document


def test_environment_examples_contain_no_assigned_credentials() -> None:
    _, env_prefix = _project_identity()
    provider_variables = {
        f"{env_prefix}_MONGO_DSN",
        f"{env_prefix}_MONGO_DATABASE",
        f"{env_prefix}_REDIS_DSN",
        f"{env_prefix}_POSTGRES_DSN",
        f"{env_prefix}_QDRANT_URL",
        f"{env_prefix}_QDRANT_API_KEY",
    }
    for path in (ROOT / "docker").glob(".env.*.example"):
        values = dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )
        assert values[f"{env_prefix}_SERVICE_BEARER_TOKEN"] == ""
        assert values[f"{env_prefix}_AI_API_KEY"] == ""
        assert provider_variables <= values.keys()
        assert all(values[name] == "" for name in provider_variables)


def test_gitlab_pipeline_has_no_push_or_deploy_commands() -> None:
    path = ROOT / ".gitlab-ci.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert document["stages"] == [
        "quality",
        "test",
        "package",
        "artifact-test",
        "container-smoke",
    ]
    content = path.read_text(encoding="utf-8").casefold()
    assert "docker push" not in content
    assert "kubectl" not in content
    assert "helm " not in content
    assert "RUN_REAL_LLM_SMOKE" in path.read_text(encoding="utf-8")
    assert "until wget" not in content
    assert "seq 1 30" in content
    assert '"(healthy|degraded)"' in content
    assert "docker logs nexus-kernel-api" in content
    after_script = "\n".join(document["container-smoke"]["after_script"])
    assert "docker logs nexus-kernel-api" in after_script
    assert "docker logs nexus-kernel-worker" in after_script
    assert after_script.index("docker logs") < after_script.index("docker rm")


def test_gitlab_tests_all_supported_python_versions() -> None:
    document = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    assert document["test"]["parallel"]["matrix"] == [
        {"PYTHON_VERSION": ["3.11", "3.12", "3.13"]}
    ]
    assert document["test"]["image"] == "python:${PYTHON_VERSION}-slim"
    assert "python -m pip check" in document["test"]["script"]
    assert document["macos-test"]["tags"] == ["macos"]
    assert document["macos-test"].get("allow_failure") is not True
    assert any(".venv-macos" in command for command in document["macos-test"]["script"])


def test_gitlab_builds_and_validates_a_clean_wheel() -> None:
    document = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    commands = "\n".join(document["wheel"]["script"])

    assert "rm -rf build dist *.egg-info" in commands
    assert "python -m build --wheel --outdir dist" in commands
    assert "python scripts/verify_wheel.py dist" in commands


def test_gitlab_installs_built_wheel_with_all_runtime_extras_on_all_pythons() -> None:
    document = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    job = document["wheel-runtime-extras"]
    commands = "\n".join(job["script"])

    assert job["stage"] == "artifact-test"
    assert job["image"] == "python:${PYTHON_VERSION}-slim"
    assert job["parallel"]["matrix"] == [{"PYTHON_VERSION": ["3.11", "3.12", "3.13"]}]
    assert job["needs"] == [{"job": "wheel", "artifacts": True}]
    assert job["before_script"] == []
    assert "find dist" in commands
    assert "[ai,mongo,redis,qdrant,otel,postgres]" in commands
    assert all(
        module in commands
        for module in (
            "asyncpg",
            "google.adk",
            "litellm",
            "openai",
            "pymongo",
            "qdrant_client",
            "redis",
            "opentelemetry.sdk",
        )
    )
    assert "python -m pip check" in job["script"]


def test_gitlab_installs_core_only_wheel_on_all_pythons() -> None:
    document = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    job = document["wheel-core-only"]
    commands = "\n".join(job["script"])

    assert job["stage"] == "artifact-test"
    assert job["parallel"]["matrix"] == [{"PYTHON_VERSION": ["3.11", "3.12", "3.13"]}]
    assert "AgentToolSpec" in commands
    assert "nexus_kernel.infrastructure" in commands
    assert "python -m pip check" in job["script"]


def test_gitlab_container_smoke_builds_all_runtime_extras() -> None:
    document = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    commands = "\n".join(document["container-smoke"]["script"])

    distribution, env_prefix = _project_identity()
    assert (
        f"--build-arg {env_prefix}_RUNTIME_EXTRAS={','.join(RUNTIME_EXTRAS)}"
        in commands
    )
    assert (
        f"docker build -f docker/Dockerfile -t {distribution}:ci-default ." in commands
    )
    assert "docker run --rm --read-only --tmpfs /tmp" in commands


def test_gitlab_quality_checks_project_python_tools() -> None:
    document = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    commands = "\n".join(document["quality"]["script"])

    assert "python -m compileall -q nexus_kernel app scripts" in commands
    assert "ruff check --select C901 nexus_kernel app scripts" in commands
    assert "git diff --check" in commands
    assert (
        "mypy nexus_kernel app scripts/init_project.py "
        "scripts/check_python_version.py scripts/verify_wheel.py"
    ) in commands
