from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _bash() -> str:
    candidates = [
        r"D:\Program Files\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("Bash is required for the full-template initializer test")


def test_full_repository_copy_initializes_consistently(tmp_path: Path) -> None:
    target = tmp_path / "template"
    shutil.copytree(
        ROOT,
        target,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv-win",
            ".venv-linux",
            ".venv-macos",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "__pycache__",
            "dist",
            "build",
            "*.egg-info",
        ),
    )
    repository = subprocess.run(
        ["git", "init", "--quiet"],
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert repository.returncode == 0, repository.stderr
    inventory = subprocess.run(
        ["git", "add", "--all"],
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert inventory.returncode == 0, inventory.stderr
    arguments = [
        _bash(),
        "scripts/init_project.sh",
        "--name",
        "Atlas Service",
        "--slug",
        "atlas-service",
        "--env-prefix",
        "ATLAS",
        "--port",
        "9124",
    ]
    skill_metadata = {
        path: path.read_bytes()
        for path in (target / ".codex/skills").rglob("agents/openai.yaml")
    }

    first = subprocess.run(
        arguments,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    second = subprocess.run(
        arguments,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "already initialized" in second.stderr
    assert 'name = "atlas-service"' in (target / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'include = ["nexus_kernel*", "app*"]' in (
        target / "pyproject.toml"
    ).read_text(encoding="utf-8")
    application = (target / "config/application.yaml").read_text(encoding="utf-8")
    modules = (target / "config/modules.yaml").read_text(encoding="utf-8")
    providers = (target / "config/providers.yaml").read_text(encoding="utf-8")
    readme = (target / "README.md").read_text(encoding="utf-8")
    dockerfile = (target / "docker/Dockerfile").read_text(encoding="utf-8")
    compose = (target / "docker/docker-compose-dev.yml").read_text(encoding="utf-8")
    pipeline = (target / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert "${ATLAS_PORT:-9124}" in application
    assert "ATLAS_AI_API_KEY" in modules
    assert "ATLAS_MONGO_DSN" in providers
    assert "ATLAS_POSTGRES_DSN" in providers
    assert "--port 9124" in readme
    assert "127.0.0.1:9124" in readme
    assert "EXPOSE 9124" in dockerfile
    assert "ARG ATLAS_RUNTIME_EXTRAS=ai" in dockerfile
    assert "${ATLAS_PORT:-9124}:${ATLAS_PORT:-9124}" in compose
    for role in ("default-api", "default-worker", "full-api", "full-worker"):
        assert f"atlas-service-{role}" in pipeline
    assert "nexus-kernel-starter" not in pipeline
    product_documents = [
        target / "AGENTS.md",
        target / "README.md",
        target / "docker/README.md",
        *(target / ".codex/skills").rglob("SKILL.md"),
        *(
            path
            for path in (target / "docs").rglob("*.md")
            if "superpowers" not in path.parts
        ),
    ]
    documentation = "\n".join(
        path.read_text(encoding="utf-8") for path in product_documents
    )
    for old_value in (
        "NexusKernel Starter",
        "nexus-kernel-starter",
        "NEXUS_KERNEL",
        "8030",
    ):
        assert old_value not in documentation
    for new_value in ("Atlas Service", "atlas-service", "ATLAS", "9124"):
        assert new_value in documentation
    assert "nexus_kernel" in documentation
    assert {path: path.read_bytes() for path in skill_metadata} == skill_metadata
    for profile in ("dev", "preprod", "prod"):
        generated = target / f"docker/.env.{profile}"
        assert generated.is_file()
        assert not generated.is_symlink()
        assert "ATLAS_PORT=9124" in generated.read_text(encoding="utf-8")
        if os.name != "nt":
            assert generated.stat().st_mode & 0o777 == 0o600
    marker = json.loads(
        (target / ".nexus-kernel-initialized").read_text(encoding="utf-8")
    )
    assert marker["slug"] == "atlas-service"
    isolated_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("NEXUS_KERNEL_", "ATLAS_"))
        and key != "RUN_REAL_LLM_SMOKE"
    }
    isolated_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    security_result = subprocess.run(
        [sys.executable, "scripts/security_scan.py"],
        cwd=target,
        env=isolated_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert security_result.returncode == 0, security_result.stderr

    docker_cli = shutil.which("docker")
    if docker_cli is not None:
        compose_environment = {
            **isolated_environment,
            "ATLAS_IMAGE": "atlas-service:initializer-contract",
        }
        for profile in ("dev", "preprod", "prod"):
            compose_result = subprocess.run(
                [
                    docker_cli,
                    "compose",
                    "--env-file",
                    f"docker/.env.{profile}",
                    "-f",
                    f"docker/docker-compose-{profile}.yml",
                    "config",
                    "--quiet",
                ],
                cwd=target,
                env=compose_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=60,
            )
            assert compose_result.returncode == 0, compose_result.stderr
    else:
        for profile in ("dev", "preprod", "prod"):
            assert f"--env-file docker/.env.{profile}.example" in pipeline

    contract_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "not real_llm",
            "-p",
            "no:cacheprovider",
            "--ignore=tests/scripts/test_full_template_init.py",
            "-q",
        ],
        cwd=target,
        env=isolated_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=600,
    )
    assert contract_result.returncode == 0, (
        contract_result.stdout + contract_result.stderr
    )

    for generated in (target / "build", target / "dist"):
        shutil.rmtree(generated, ignore_errors=True)
    for egg_info in target.glob("*.egg-info"):
        shutil.rmtree(egg_info)
    build_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            "dist",
        ],
        cwd=target,
        env=isolated_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=180,
    )
    assert build_result.returncode == 0, build_result.stdout + build_result.stderr
    assert [path.name for path in (target / "dist").glob("*.whl")] == [
        "atlas_service-0.2.0-py3-none-any.whl"
    ]
    wheel_result = subprocess.run(
        [sys.executable, "scripts/verify_wheel.py", "dist"],
        cwd=target,
        env=isolated_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert wheel_result.returncode == 0, wheel_result.stderr
