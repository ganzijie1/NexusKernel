from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASH = Path(r"D:\Program Files\Git\bin\bash.exe")

CORE_DEPENDENCIES = [
    "fastapi==0.139.0",
    "uvicorn[standard]==0.51.0",
    "pydantic==2.13.4",
    "PyYAML==6.0.3",
    "prometheus-client==0.25.0",
    "loguru==0.7.3",
    "httpx==0.28.1",
]

OPTIONAL_DEPENDENCIES = {
    "ai": ["google-adk==2.1.0", "litellm==1.87.0", "openai==2.40.0"],
    "mongo": ["pymongo==4.17.0"],
    "redis": ["redis==8.0.1"],
    "qdrant": ["qdrant-client==1.18.0"],
    "otel": [
        "opentelemetry-api==1.43.0",
        "opentelemetry-sdk==1.43.0",
        "opentelemetry-exporter-otlp-proto-grpc==1.43.0",
    ],
    "dev": [
        "pytest==9.1.1",
        "pytest-asyncio==1.4.0",
        "ruff==0.15.21",
        "mypy==2.2.0",
    ],
}


def _required_file(relative_path: str) -> Path:
    path = ROOT / relative_path
    assert path.is_file(), f"missing required project file: {relative_path}"
    return path


def _load_pyproject() -> dict[str, object]:
    with _required_file("pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def _meaningful_lines(relative_path: str) -> set[str]:
    lines = _required_file(relative_path).read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}


def _git_ignores(relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", relative_path],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode == 0


def test_pyproject_defines_build_and_core_project_contract() -> None:
    config = _load_pyproject()

    build_system = config["build-system"]
    assert build_system["build-backend"] == "setuptools.build_meta"
    assert any(requirement.startswith("setuptools") for requirement in build_system["requires"])

    project = config["project"]
    assert project["name"] == "nexuskernel-starter"
    assert project["requires-python"] == ">=3.11,<3.12"
    assert project["dependencies"] == CORE_DEPENDENCIES


def test_pyproject_defines_exact_optional_dependency_groups() -> None:
    project = _load_pyproject()["project"]

    assert project["optional-dependencies"] == OPTIONAL_DEPENDENCIES


def test_pyproject_discovers_packages_and_configures_tools() -> None:
    config = _load_pyproject()

    assert config["tool"]["setuptools"]["packages"]["find"]["include"] == [
        "nexuskernel*",
        "app*",
    ]
    assert config["tool"]["pytest"]["ini_options"]["asyncio_mode"] == "auto"
    assert config["tool"]["ruff"]["target-version"] == "py311"
    assert config["tool"]["mypy"]["files"] == ["app", "nexuskernel"]


def test_import_package_roots_exist() -> None:
    assert _required_file("nexuskernel/__init__.py")
    assert _required_file("app/__init__.py")


def test_setup_script_has_strict_python_311_environment_contract() -> None:
    script_path = _required_file("scripts/setup_python_env.sh")
    script = script_path.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert all(option in script for option in ("--python", "--recreate", "--help"))
    assert all(platform in script for platform in ("MINGW*", "MSYS*", "CYGWIN*", "Linux*"))
    assert all(environment in script for environment in (".venv-win", ".venv-linux"))
    assert "3.11" in script
    assert "pip install --upgrade pip" in script
    assert '--editable ".[ai,dev]"' in script
    assert "rm -rf --" in script
    assert "$project_root/.venv-win" in script
    assert "$project_root/.venv-linux" in script
    assert re.search(r"(?<![\w-])\.venv(?![\w-])", script) is None

    syntax = subprocess.run(
        [str(BASH), "-n", "scripts/setup_python_env.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        [str(BASH), "scripts/setup_python_env.sh", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert all(option in help_result.stdout for option in ("--python", "--recreate", "--help"))


def test_gitignore_excludes_generated_and_secret_files_but_allows_examples() -> None:
    _required_file(".gitignore")

    ignored = [
        ".venv-win/pyvenv.cfg",
        ".venv-linux/pyvenv.cfg",
        "app/__pycache__/module.cpython-311.pyc",
        ".pytest_cache/state",
        ".mypy_cache/state",
        ".ruff_cache/state",
        "build/artifact.whl",
        "dist/artifact.whl",
        "runtime.log",
        "logs/runtime.log",
        ".env",
        ".env.local",
        "docker/.env.prod",
    ]
    allowed = [".env.example", ".env.local.example", "docker/.env.prod.example"]

    assert all(_git_ignores(path) for path in ignored)
    assert all(not _git_ignores(path) for path in allowed)


def test_dockerignore_excludes_repository_only_and_sensitive_paths() -> None:
    rules = _meaningful_lines(".dockerignore")

    assert {
        ".git",
        ".venv-win",
        ".venv-linux",
        "**/__pycache__",
        "**/.pytest_cache",
        "**/.mypy_cache",
        "**/.ruff_cache",
        ".env",
        ".env.*",
        "!*.example",
    } <= rules


def test_gitattributes_normalizes_cross_platform_line_endings() -> None:
    rules = _meaningful_lines(".gitattributes")

    assert "* text=auto eol=lf" in rules
    assert "*.bat text eol=crlf" in rules
    assert "*.cmd text eol=crlf" in rules
    assert "*.ps1 text eol=crlf" in rules


def test_repository_has_no_requirements_manifest_or_plain_venv_contract() -> None:
    requirements_files = [
        path.relative_to(ROOT)
        for path in ROOT.rglob("requirements*.txt")
        if not {".venv-win", ".venv-linux"}.intersection(path.relative_to(ROOT).parts)
    ]
    assert requirements_files == []
    assert not (ROOT / ".venv").exists()
