from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_is_multistage_non_root_and_role_driven() -> None:
    content = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert content.count("FROM ") >= 2
    assert "USER 10001:10001" in content
    assert 'CMD ["python", "-m", "app.launcher"]' in content
    assert "COPY ." not in content


def test_compose_files_are_parseable_and_have_no_external_or_fixed_networks() -> None:
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
            document["services"]["api"]["environment"]["NEXUSKERNEL_RUNTIME_ROLE"]
            == "api"
        )
        assert (
            document["services"]["worker"]["environment"]["NEXUSKERNEL_RUNTIME_ROLE"]
            == "worker"
        )
        serialized = path.read_text(encoding="utf-8")
        assert "ipv4_address" not in serialized
        assert "external:" not in serialized
        assert "password:" not in serialized.casefold()


def test_environment_examples_contain_no_assigned_credentials() -> None:
    for path in (ROOT / "docker").glob(".env.*.example"):
        values = dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )
        assert values["NEXUSKERNEL_SERVICE_BEARER_TOKEN"] == ""
        assert values["NEXUSKERNEL_AI_API_KEY"] == ""


def test_gitlab_pipeline_has_no_push_or_deploy_commands() -> None:
    path = ROOT / ".gitlab-ci.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert document["stages"] == ["quality", "test", "package", "container-smoke"]
    content = path.read_text(encoding="utf-8").casefold()
    assert "docker push" not in content
    assert "kubectl" not in content
    assert "helm " not in content
    assert "RUN_REAL_LLM_SMOKE" in path.read_text(encoding="utf-8")
