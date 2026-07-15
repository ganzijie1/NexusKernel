from __future__ import annotations

import json
import shutil
import subprocess
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
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "__pycache__",
            "dist",
            "build",
            "*.egg-info",
        ),
    )
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

    first = subprocess.run(
        arguments,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    second = subprocess.run(
        arguments,
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "already initialized" in second.stderr
    assert 'name = "atlas-service"' in (target / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'include = ["nexuskernel*", "app*"]' in (
        target / "pyproject.toml"
    ).read_text(encoding="utf-8")
    application = (target / "config/application.yaml").read_text(encoding="utf-8")
    modules = (target / "config/modules.yaml").read_text(encoding="utf-8")
    readme = (target / "README.md").read_text(encoding="utf-8")
    dockerfile = (target / "docker/Dockerfile").read_text(encoding="utf-8")
    compose = (target / "docker/docker-compose-dev.yml").read_text(encoding="utf-8")
    assert "${ATLAS_PORT:-9124}" in application
    assert "ATLAS_AI_API_KEY" in modules
    assert "--port 9124" in readme
    assert "127.0.0.1:9124" in readme
    assert "EXPOSE 9124" in dockerfile
    assert "${ATLAS_PORT:-9124}:${ATLAS_PORT:-9124}" in compose
    for profile in ("dev", "preprod", "prod"):
        generated = target / f"docker/.env.{profile}"
        assert generated.is_file()
        assert "ATLAS_PORT=9124" in generated.read_text(encoding="utf-8")
    marker = json.loads(
        (target / ".nexuskernel-initialized").read_text(encoding="utf-8")
    )
    assert marker["slug"] == "atlas-service"
