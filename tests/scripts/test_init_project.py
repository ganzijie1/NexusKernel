from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
PYTHON_INITIALIZER = ROOT / "scripts/init_project.py"
BASH_INITIALIZER = ROOT / "scripts/init_project.sh"
VALID_ARGUMENTS = (
    "--name",
    "Acme Intelligence",
    "--slug",
    "acme-intelligence",
    "--env-prefix",
    "ACME_AI",
    "--port",
    "9123",
)


def _find_bash() -> str:
    candidates: list[Path] = []
    if os.name == "nt":
        candidates.append(Path(ROOT.anchor) / "Program Files/Git/bin/bash.exe")
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            if value := os.environ.get(variable):
                candidates.append(Path(value) / "Git/bin/bash.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    if discovered := shutil.which("bash"):
        return discovered
    pytest.skip("Bash is required to verify scripts/init_project.sh")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def _make_template(tmp_path: Path) -> Path:
    project = tmp_path / "template"
    _write(
        project / "README.md",
        """# NexusKernel Starter

Distribution: nexuskernel-starter
Environment: NEXUSKERNEL_API_KEY
Package: nexuskernel
NEXUSKERNEL_DEFAULT_PORT=8030
Historical example: 8030
Unrelated assignment: OTHER_DEFAULT_PORT=8030
Embedded display text: BeforeNexusKernel StarterAfter
""",
    )
    _write(
        project / "pyproject.toml",
        """[project]
name = "nexuskernel-starter"

[tool.setuptools.packages.find]
include = ["nexuskernel*"]
""",
    )
    _write(
        project / "config/settings.yaml",
        """service:
  name: NexusKernel Starter
  env_prefix: NEXUSKERNEL
  port: 8030
  sample_year: 8030
""",
    )
    _write(
        project / "docker/Dockerfile",
        "ENV NEXUSKERNEL_DEFAULT_PORT=8030\n",
    )
    _write(
        project / "docker/docker-compose-dev.yml",
        """services:
  api:
    ports:
      - "${NEXUSKERNEL_PORT:-8030}:8030"
      - "${OTHER_PORT:-8030}:8030"
    labels:
      sample-port: "8030"
""",
    )
    _write(
        project / "docker/.env.dev.example",
        """NEXUSKERNEL_DEFAULT_PORT=8030
NEXUSKERNEL_API_KEY=
DATABASE_URL=${DATABASE_URL:-}
HOST=localhost
""",
    )
    _write(
        project / "docker/README.md",
        "Run NexusKernel Starter with NEXUSKERNEL_DEFAULT_PORT=8030.\n",
    )
    _write(
        project / ".gitlab-ci.yml",
        'variables:\n  NEXUSKERNEL_DEFAULT_PORT: "8030"\n',
    )
    _write(
        project / "docs/guide/nested.md",
        "NexusKernel Starter uses nexuskernel-starter and NEXUSKERNEL.\n",
    )
    _write(
        project / "scripts/untouched.py",
        'NAME = "NexusKernel Starter"\nPORT = 8030\n',
    )
    _write(
        project / "tests/untouched.md",
        "NexusKernel Starter nexuskernel-starter NEXUSKERNEL 8030\n",
    )
    _write(
        project / "outside-whitelist.txt",
        "NexusKernel Starter nexuskernel-starter NEXUSKERNEL 8030\n",
    )
    binary_doc = project / "docs/binary.md"
    binary_doc.parent.mkdir(parents=True, exist_ok=True)
    binary_doc.write_bytes(b"\xff\x00NexusKernel Starter\x80")
    return project


def _copy_initializer(project: Path) -> None:
    if not PYTHON_INITIALIZER.is_file() or not BASH_INITIALIZER.is_file():
        pytest.skip("initializer entry points are absent during the expected RED run")
    scripts = project / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PYTHON_INITIALIZER, scripts / PYTHON_INITIALIZER.name)
    shutil.copy2(BASH_INITIALIZER, scripts / BASH_INITIALIZER.name)


@pytest.fixture
def template(tmp_path: Path) -> Path:
    project = _make_template(tmp_path)
    _copy_initializer(project)
    return project


def _run_initializer(
    project: Path, arguments: tuple[str, ...] = VALID_ARGUMENTS
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(project / "scripts/init_project.py"), *arguments],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )


def _snapshot(project: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(project).as_posix(): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in project.rglob("*")
        if path.is_file()
    }


def _load_initializer(project: Path) -> ModuleType:
    module_name = f"init_project_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name, project / "scripts/init_project.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_initializer_entry_points_exist() -> None:
    assert PYTHON_INITIALIZER.is_file(), "missing scripts/init_project.py"
    assert BASH_INITIALIZER.is_file(), "missing scripts/init_project.sh"


def test_bash_entry_point_is_strict_and_only_delegates_to_python() -> None:
    assert BASH_INITIALIZER.is_file(), "missing scripts/init_project.sh"
    source = BASH_INITIALIZER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert all(candidate in source for candidate in ("python3.11", "python3", "python"))
    assert "${PYTHON:-}" in source
    assert 'PATH="/usr/bin:/bin${PATH:+:$PATH}"' in source
    assert "init_project.py" in source
    assert '"$@"' in source
    assert "venv" not in source.lower()

    environment = os.environ.copy()
    environment["PYTHON"] = sys.executable
    environment["PATH"] = str(Path(sys.executable).parent)
    result = subprocess.run(
        [_find_bash(), "scripts/init_project.sh", "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert all(option in result.stdout for option in VALID_ARGUMENTS[::2])


def test_valid_initialization_replaces_identity_and_generates_env(
    template: Path,
) -> None:
    result = _run_initializer(
        template,
        (
            "--name",
            "  Acme Intelligence  ",
            "--slug",
            "acme-intelligence",
            "--env-prefix",
            "ACME_AI",
            "--port",
            "9123",
        ),
    )

    assert result.returncode == 0, result.stderr
    readme = (template / "README.md").read_text(encoding="utf-8")
    pyproject = (template / "pyproject.toml").read_text(encoding="utf-8")
    config = (template / "config/settings.yaml").read_text(encoding="utf-8")
    compose = (template / "docker/docker-compose-dev.yml").read_text(encoding="utf-8")
    generated_env = (template / "docker/.env.dev").read_text(encoding="utf-8")
    nested_doc = (template / "docs/guide/nested.md").read_text(encoding="utf-8")
    marker = json.loads(
        (template / ".nexuskernel-initialized").read_text(encoding="utf-8")
    )

    assert "# Acme Intelligence" in readme
    assert "acme-intelligence" in readme and "ACME_AI_API_KEY" in readme
    assert "NEXUSKERNEL_DEFAULT_PORT=8030" not in readme
    assert "ACME_AI_DEFAULT_PORT=9123" in readme
    assert "Historical example: 8030" in readme
    assert "Unrelated assignment: OTHER_DEFAULT_PORT=8030" in readme
    assert "Embedded display text: BeforeNexusKernel StarterAfter" in readme
    assert 'name = "acme-intelligence"' in pyproject
    assert 'include = ["nexuskernel*"]' in pyproject
    assert "  port: 9123" in config
    assert "  sample_year: 8030" in config
    assert "${ACME_AI_PORT:-9123}:8030" in compose
    assert "${OTHER_PORT:-8030}:8030" in compose
    assert 'sample-port: "8030"' in compose
    assert "ACME_AI_DEFAULT_PORT=9123" in generated_env
    assert "ACME_AI_API_KEY=" in generated_env
    assert "Acme Intelligence" in nested_doc
    assert "nexuskernel" in readme
    assert (template / "docs/binary.md").read_bytes().startswith(b"\xff\x00")
    assert marker == {
        "name": "Acme Intelligence",
        "slug": "acme-intelligence",
        "env_prefix": "ACME_AI",
        "port": 9123,
    }


@pytest.mark.parametrize(
    ("arguments", "invalid_value"),
    [
        (("--name", "   "), "name"),
        (("--name", "line one\nline two"), "name"),
        (("--name", "x" * 101), "name"),
        (("--slug", "Upper-Case"), "slug"),
        (("--slug", "bad--slug"), "slug"),
        (("--slug", "-bad"), "slug"),
        (("--slug", "bad-"), "slug"),
        (("--env-prefix", "lower_case"), "env-prefix"),
        (("--env-prefix", "1INVALID"), "env-prefix"),
        (("--env-prefix", "INVALID-PREFIX"), "env-prefix"),
        (("--port", "0"), "port"),
        (("--port", "65536"), "port"),
        (("--port", "not-an-integer"), "port"),
    ],
)
def test_invalid_values_are_rejected_without_writes(
    template: Path,
    arguments: tuple[str, str],
    invalid_value: str,
) -> None:
    values = dict(zip(VALID_ARGUMENTS[::2], VALID_ARGUMENTS[1::2], strict=True))
    values[arguments[0]] = arguments[1]
    command = tuple(item for pair in values.items() for item in pair)
    before = _snapshot(template)

    result = _run_initializer(template, command)

    assert result.returncode != 0
    assert invalid_value in result.stderr.lower()
    assert _snapshot(template) == before


@pytest.mark.parametrize("missing", ["README.md", "pyproject.toml"])
def test_missing_required_root_file_is_rejected_without_writes(
    template: Path, missing: str
) -> None:
    (template / missing).unlink()
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert missing.lower() in result.stderr.lower()
    assert _snapshot(template) == before


def test_second_execution_is_rejected_without_changes(template: Path) -> None:
    first = _run_initializer(template)
    assert first.returncode == 0, first.stderr
    before = _snapshot(template)

    second = _run_initializer(
        template,
        (
            "--name",
            "Other Project",
            "--slug",
            "other-project",
            "--env-prefix",
            "OTHER",
            "--port",
            "8123",
        ),
    )

    assert second.returncode != 0
    assert "initialized" in second.stderr.lower()
    assert _snapshot(template) == before


def test_existing_real_env_is_rejected_without_any_write(template: Path) -> None:
    _write(template / "docker/.env.dev", "EXISTING=value\n")
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert "docker/.env.dev" in result.stderr.replace("\\", "/")
    assert _snapshot(template) == before
    assert not (template / ".nexuskernel-initialized").exists()


def test_replace_failure_restores_files_env_and_marker(
    template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_initializer(template)
    before = _snapshot(template)
    real_replace = os.replace

    def fail_marker_replace(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        if Path(target).name == ".nexuskernel-initialized":
            real_replace(source, target)
            raise OSError("injected marker replace failure")
        real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_marker_replace)
    identity = module.ProjectIdentity(
        name="Acme Intelligence",
        slug="acme-intelligence",
        env_prefix="ACME_AI",
        port=9123,
    )

    with pytest.raises(OSError, match="injected marker replace failure"):
        module.initialize_project(template, identity)

    assert _snapshot(template) == before
    assert not (template / "docker/.env.dev").exists()
    assert not (template / ".nexuskernel-initialized").exists()
    assert not list(template.rglob("*.tmp"))


def test_modified_file_modes_are_preserved(template: Path) -> None:
    original_modes = {
        path: stat.S_IMODE(path.stat().st_mode)
        for path in (
            template / "README.md",
            template / "pyproject.toml",
            template / "config/settings.yaml",
        )
    }

    result = _run_initializer(template)

    assert result.returncode == 0, result.stderr
    assert {
        path: stat.S_IMODE(path.stat().st_mode) for path in original_modes
    } == original_modes


def test_non_whitelisted_files_are_never_modified(template: Path) -> None:
    protected = {
        path: path.read_bytes()
        for path in (
            template / "scripts/untouched.py",
            template / "tests/untouched.md",
            template / "outside-whitelist.txt",
        )
    }

    result = _run_initializer(template)

    assert result.returncode == 0, result.stderr
    assert {path: path.read_bytes() for path in protected} == protected
