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
DEV_INFRA_IMAGES = {
    "mongo": (
        "mongo:8.0@sha256:"
        "3ce3de7f40e914034b03b7dec654005ab54f7dc8306937e44ec6760d9e9409a1"
    ),
    "redis": (
        "redis:8.0-alpine@sha256:"
        "5f61955be8ab2ccee9372b84ae4d4da2e2b156f87281e3f218544055e7ee04d4"
    ),
}
PYTHON_IMAGE_MATRIX = [
    {
        "PYTHON_VERSION": ["3.11"],
        "PYTHON_IMAGE": [
            "python:3.11-slim@sha256:"
            "db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93"
        ],
    },
    {
        "PYTHON_VERSION": ["3.12"],
        "PYTHON_IMAGE": [
            "python:3.12-slim@sha256:"
            "57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
        ],
    },
    {
        "PYTHON_VERSION": ["3.13"],
        "PYTHON_IMAGE": [
            "python:3.13-slim@sha256:"
            "6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
        ],
    },
]


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
    assert (
        content.count(
            "FROM python:3.11-slim@sha256:"
            "db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93"
        )
        == 2
    )
    assert "groupadd --gid 10001 nexus_kernel" in content
    assert "useradd --uid 10001 --gid nexus_kernel" in content
    assert "USER 10001:10001" in content
    assert "COPY --chown=nexus_kernel:nexus_kernel config ./config" in content
    assert 'CMD ["python", "-m", "app.launcher"]' in content
    assert "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false" in content
    assert "LITELLM_LOCAL_MODEL_COST_MAP=true" in content
    assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT" in content
    assert "COPY ." not in content
    assert "PIP_DEFAULT_TIMEOUT=120" in content
    assert content.startswith(f"ARG {argument}=ai\n")
    assert content.count(f"ARG {argument}") == 3
    builder, runtime = content.split("FROM ")[1:]
    assert f"ARG {argument}" in builder
    assert f'".[${{{argument}}}]"' in builder
    assert "COPY constraints/py311-313.txt ./constraints/py311-313.txt" in content
    assert (
        "--mount=type=secret,id=pip_config,target=/etc/pip.conf,required=false"
        in builder
    )
    assert "pip wheel --timeout 120 --retries 8" in builder
    assert "--constraint constraints/py311-313.txt" in builder
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

        healthcheck = document["services"]["api"]["healthcheck"]
        assert healthcheck["timeout"] == "2s"
        assert healthcheck["retries"] == 5
        assert healthcheck["test"][:3] == ["CMD", "python", "-c"]
        probe = healthcheck["test"][3]
        assert "/health/live" in probe
        assert "urlopen(" in probe
        assert "timeout=2" in probe


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
    assert {
        service: dev["services"][service]["image"] for service in DEV_INFRA_IMAGES
    } == DEV_INFRA_IMAGES

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

    for profile in ("preprod", "prod"):
        document = yaml.safe_load(
            (ROOT / f"docker/docker-compose-{profile}.yml").read_text(encoding="utf-8")
        )
        for role in ("api", "worker"):
            service = document["services"][role]
            assert service["user"] == "10001:10001"
            assert service["read_only"] is True
            assert service["tmpfs"] == ["/tmp"]
            assert service["security_opt"] == ["no-new-privileges:true"]


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
        assert values["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"
        assert values["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"
        assert (
            values["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "NO_CONTENT"
        )
        assert provider_variables <= values.keys()
        assert all(values[name] == "" for name in provider_variables)


def test_gitlab_pipeline_has_no_push_or_deploy_commands() -> None:
    path = ROOT / ".gitlab-ci.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    distribution, _ = _project_identity()

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
    assert path.read_text(encoding="utf-8").count("wget -q -T 2 -t 1") == 6
    assert "json.load(sys.stdin)" in content
    assert "grep -eq" not in content
    assert f"docker logs {distribution}-default-api" in content
    after_script = "\n".join(document["container-smoke"]["after_script"])
    for container in (
        f"{distribution}-default-api",
        f"{distribution}-default-worker",
        f"{distribution}-full-api",
        f"{distribution}-full-worker",
    ):
        assert f"docker logs {container}" in after_script
        assert container in after_script
    assert after_script.index("docker logs") < after_script.index("docker rm")


def test_gitlab_tests_all_supported_python_versions() -> None:
    document = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    assert document["default"]["image"] == PYTHON_IMAGE_MATRIX[0]["PYTHON_IMAGE"][0]
    assert document["test"]["parallel"]["matrix"] == PYTHON_IMAGE_MATRIX
    assert document["test"]["image"] == "$PYTHON_IMAGE"
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
    assert job["image"] == "$PYTHON_IMAGE"
    assert job["parallel"]["matrix"] == PYTHON_IMAGE_MATRIX
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
    assert job["parallel"]["matrix"] == PYTHON_IMAGE_MATRIX
    assert "AgentToolSpec" in commands
    assert "nexus_kernel.infrastructure" in commands
    assert "('google','httpx','httpx2','litellm'" in commands
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
    assert document["container-smoke"]["timeout"] == "15m"
    assert document["container-smoke"]["image"] == (
        "docker:27.5.1-cli@sha256:"
        "851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c"
    )
    assert document["container-smoke"]["services"] == [
        {
            "name": (
                "docker:27.5.1-dind@sha256:"
                "aa3df78ecf320f5fafdce71c659f1629e96e9de0968305fe1de670e0ca9176ce"
            )
        }
    ]
    assert document["variables"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"
    assert document["variables"]["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"
    assert (
        document["variables"]["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"]
        == "NO_CONTENT"
    )
    for image in (f"{distribution}:ci-default", f"{distribution}:ci"):
        assert f'docker run --rm {image} id -u)" = "10001"' in commands
        assert f'docker run --rm {image} id -g)" = "10001"' in commands
        assert (
            "docker run --rm --read-only --tmpfs /tmp "
            f"--security-opt no-new-privileges:true {image} python -m pip check"
        ) in commands
    assert all(
        f"--name {distribution}-{variant}-{role} --read-only --tmpfs /tmp "
        "--security-opt no-new-privileges:true" in commands
        for variant in ("default", "full")
        for role in ("api", "worker")
    )
    assert commands.count("/health/live") == 4
    assert commands.count("/health/ready") == 2
    assert commands.count(f"{env_prefix}_RUNTIME_ROLE=worker") == 2
    assert "8031" not in commands
    assert commands.count(f"${env_prefix}_DEFAULT_PORT/health/live") == 4
    assert commands.count("{{.State.ExitCode}}") == 4
    assert "docker run --rm -d --name" not in commands
    assert (
        "import app, google.adk, google.genai, litellm, nexus_kernel, openai, starlette"
        in commands
    )
    assert "import asyncpg, google.adk, google.genai, litellm, openai" in commands
    assert commands.count("json.load(sys.stdin)") == 4
    assert commands.count("payload.get('data') == {'status':'alive'}") == 2
    assert commands.count("data.get('status') in {'healthy','degraded'}") == 2
    assert "docker image inspect --format '{{.Size}}'" in commands
    assert 'test "$DEFAULT_IMAGE_SIZE_BYTES" -le 498073600' in commands
    assert 'test "$FULL_IMAGE_SIZE_BYTES" -le 671088640' in commands
    assert commands.count("digest={{.Id}} size_bytes={{.Size}}") == 2
    assert commands.count("wget -q -T 2 -t 1") == 6


def test_gitlab_quality_checks_project_python_tools() -> None:
    document = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    commands = "\n".join(document["quality"]["script"])

    assert "python -m compileall -q nexus_kernel app scripts" in commands
    assert "ruff check --select C901 nexus_kernel app scripts" in commands
    assert "git diff --check" not in commands
    assert "python scripts/check_text_contract.py" in commands
    assert "bash scripts/compile_constraints.sh --check" in commands
    assert (
        "mypy nexus_kernel app scripts/init_project.py "
        "scripts/check_python_version.py scripts/check_text_contract.py "
        "scripts/delivery_contract.py "
        "scripts/security_scan.py scripts/verify_wheel.py"
    ) in commands
