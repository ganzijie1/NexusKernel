from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from pathspec import GitIgnoreSpec


ROOT = Path(__file__).resolve().parents[2]

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
        "pathspec==1.1.1",
        "types-PyYAML==6.0.12.20260518",
    ],
}


def _find_bash() -> str:
    if os.name == "nt":
        candidates = [Path(ROOT.anchor) / "Program Files/Git/bin/bash.exe"]
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            if value := os.environ.get(variable):
                candidates.append(Path(value) / "Git/bin/bash.exe")
        if local_app_data := os.environ.get("LOCALAPPDATA"):
            candidates.append(Path(local_app_data) / "Programs/Git/bin/bash.exe")

        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)

    if discovered := shutil.which("bash"):
        return discovered

    pytest.skip("Bash is required to verify scripts/setup_python_env.sh")


def _required_file(relative_path: str) -> Path:
    path = ROOT / relative_path
    assert path.is_file(), f"missing required project file: {relative_path}"
    return path


def _load_pyproject() -> dict[str, object]:
    with _required_file("pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def _meaningful_lines(relative_path: str) -> set[str]:
    lines = _required_file(relative_path).read_text(encoding="utf-8").splitlines()
    return {
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }


def _git_ignores(relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", relative_path],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode == 0


def _write_minimal_bootstrap_project(project_root: Path) -> None:
    script_dir = project_root / "scripts"
    package_dir = project_root / "sample"
    script_dir.mkdir(parents=True)
    package_dir.mkdir()
    shutil.copy2(_required_file("scripts/setup_python_env.sh"), script_dir)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "bootstrap_backend.py").write_text(
        """from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    project_root = Path(__file__).resolve().parent
    wheel_name = "bootstrap_test_project-0.0.0-py3-none-any.whl"
    dist_info = "bootstrap_test_project-0.0.0.dist-info"
    newline = chr(10)
    wheel = newline.join(
        [
            "Wheel-Version: 1.0",
            "Generator: bootstrap-test-backend",
            "Root-Is-Purelib: true",
            "Tag: py3-none-any",
            "",
            "",
        ]
    )
    metadata = newline.join(
        [
            "Metadata-Version: 2.1",
            "Name: bootstrap-test-project",
            "Version: 0.0.0",
            "Provides-Extra: ai",
            "Provides-Extra: dev",
            "",
            "",
        ]
    )
    entries = [
        "bootstrap_test_project.pth",
        f"{dist_info}/WHEEL",
        f"{dist_info}/METADATA",
        f"{dist_info}/RECORD",
    ]
    record = newline.join(f"{entry},," for entry in entries) + newline
    with ZipFile(Path(wheel_directory) / wheel_name, "w", ZIP_DEFLATED) as archive:
        archive.writestr(entries[0], f"{project_root}{newline}")
        archive.writestr(entries[1], wheel)
        archive.writestr(entries[2], metadata)
        archive.writestr(entries[3], record)
    return wheel_name
""",
        encoding="utf-8",
    )
    (project_root / "pyproject.toml").write_text(
        """[build-system]
requires = []
build-backend = "bootstrap_backend"
backend-path = ["."]

[project]
name = "bootstrap-test-project"
version = "0.0.0"
requires-python = ">=3.11,<3.12"

[project.optional-dependencies]
ai = []
dev = []

[tool.setuptools.packages.find]
include = ["sample*"]
""",
        encoding="utf-8",
    )


def _run_setup(project_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
        }
    )
    return subprocess.run(
        [
            _find_bash(),
            "scripts/setup_python_env.sh",
            "--python",
            sys.executable,
            *arguments,
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _environment_layouts(project_root: Path) -> tuple[Path, Path, Path]:
    if os.name == "nt":
        environment_dir = project_root / ".venv-win"
        return (
            environment_dir,
            environment_dir / "Scripts/python.exe",
            environment_dir / "bin/python",
        )

    environment_dir = project_root / ".venv-linux"
    return (
        environment_dir,
        environment_dir / "bin/python",
        environment_dir / "Scripts/python.exe",
    )


def test_tooling_tests_resolve_bash_at_runtime() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    hardcoded_assignment = "BASH " + "= Path("

    assert hardcoded_assignment not in source


def test_pyproject_defines_build_and_core_project_contract() -> None:
    config = _load_pyproject()

    build_system = config["build-system"]
    assert build_system["build-backend"] == "setuptools.build_meta"
    assert any(
        requirement.startswith("setuptools") for requirement in build_system["requires"]
    )

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
    bash = _find_bash()

    assert "set -euo pipefail" in script
    assert all(option in script for option in ("--python", "--recreate", "--help"))
    assert all(
        platform in script for platform in ("MINGW*", "MSYS*", "CYGWIN*", "Linux*")
    )
    assert all(environment in script for environment in (".venv-win", ".venv-linux"))
    assert "3.11" in script
    assert "pip install --upgrade pip" in script
    assert '--editable ".[ai,dev]"' in script
    assert "rm -rf --" in script
    assert "$project_root/.venv-win" in script
    assert "$project_root/.venv-linux" in script
    assert "validate_venv" in script
    assert "sys.prefix" in script
    assert "sys.base_prefix" in script
    assert re.search(r"(?<![\w-])\.venv(?![\w-])", script) is None

    syntax = subprocess.run(
        [bash, "-n", "scripts/setup_python_env.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        [bash, "scripts/setup_python_env.sh", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert all(
        option in help_result.stdout for option in ("--python", "--recreate", "--help")
    )


def test_setup_script_discovers_both_virtual_environment_interpreter_layouts() -> None:
    script = _required_file("scripts/setup_python_env.sh").read_text(encoding="utf-8")

    assert re.search(r"^find_venv_python\(\)", script, flags=re.MULTILINE)
    assert "Scripts/python.exe" in script
    assert "bin/python" in script
    assert "environment_python_relative" not in script
    assert script.count('find_venv_python "$environment_dir"') >= 2


def test_setup_script_reuses_an_existing_alternate_interpreter_layout(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "alternate-layout"
    _write_minimal_bootstrap_project(project_root)

    first_run = _run_setup(project_root)
    assert first_run.returncode == 0, first_run.stderr

    _, original_python, alternate_python = _environment_layouts(project_root)
    assert original_python.is_file()
    alternate_python.parent.mkdir(parents=True)
    shutil.copy2(original_python, alternate_python)
    alternate_python.chmod(0o755)
    original_python.unlink()

    reuse = _run_setup(project_root)
    assert reuse.returncode == 0, reuse.stderr


def test_setup_script_recreate_only_replaces_the_selected_environment(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "recreate"
    _write_minimal_bootstrap_project(project_root)

    first_run = _run_setup(project_root)
    assert first_run.returncode == 0, first_run.stderr

    environment_dir, _, _ = _environment_layouts(project_root)
    environment_sentinel = environment_dir / "remove-on-recreate.txt"
    root_sentinel = project_root / "keep-in-project.txt"
    outside_sentinel = tmp_path / "keep-outside-project.txt"
    wrong_environment = project_root / (
        ".venv-linux" if os.name == "nt" else ".venv-win"
    )
    wrong_environment_sentinel = wrong_environment / "keep-wrong-environment.txt"
    wrong_environment.mkdir()
    for sentinel in (
        environment_sentinel,
        root_sentinel,
        outside_sentinel,
        wrong_environment_sentinel,
    ):
        sentinel.write_text("sentinel", encoding="utf-8")

    recreated = _run_setup(project_root, "--recreate")
    assert recreated.returncode == 0, recreated.stderr
    assert not environment_sentinel.exists()
    assert root_sentinel.read_text(encoding="utf-8") == "sentinel"
    assert outside_sentinel.read_text(encoding="utf-8") == "sentinel"
    assert wrong_environment_sentinel.read_text(encoding="utf-8") == "sentinel"

    environment_dir, scripts_python, bin_python = _environment_layouts(project_root)
    final_python = scripts_python if scripts_python.is_file() else bin_python
    validity = subprocess.run(
        [
            str(final_python),
            "-c",
            (
                "import sys; from pathlib import Path; "
                "expected = Path(sys.argv[1]).resolve(); "
                "prefix = Path(sys.prefix).resolve(); "
                "raise SystemExit(0 if prefix == expected and sys.base_prefix != sys.prefix else 1)"
            ),
            str(environment_dir),
        ],
        check=False,
    )
    assert validity.returncode == 0


def test_setup_script_recreate_damaged_environment_preserves_outside_files(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "damaged-recreate"
    _write_minimal_bootstrap_project(project_root)
    environment_dir, _, _ = _environment_layouts(project_root)
    environment_dir.mkdir()
    damaged_sentinel = environment_dir / "remove-damaged-environment.txt"
    outside_sentinel = tmp_path / "preserve-outside-damaged-project.txt"
    damaged_sentinel.write_text("damaged", encoding="utf-8")
    outside_sentinel.write_text("outside", encoding="utf-8")

    recreated = _run_setup(project_root, "--recreate")
    assert recreated.returncode == 0, recreated.stderr
    assert not damaged_sentinel.exists()
    assert outside_sentinel.read_text(encoding="utf-8") == "outside"

    environment_dir, scripts_python, bin_python = _environment_layouts(project_root)
    final_python = scripts_python if scripts_python.is_file() else bin_python
    validity = subprocess.run(
        [
            str(final_python),
            "-c",
            (
                "import sys; from pathlib import Path; "
                "expected = Path(sys.argv[1]).resolve(); "
                "prefix = Path(sys.prefix).resolve(); "
                "raise SystemExit(0 if prefix == expected and sys.base_prefix != sys.prefix else 1)"
            ),
            str(environment_dir),
        ],
        check=False,
    )
    assert validity.returncode == 0


def test_setup_script_rejects_interpreter_from_a_different_venv(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    script_dir = project_root / "scripts"
    script_dir.mkdir(parents=True)
    shutil.copy2(_required_file("scripts/setup_python_env.sh"), script_dir)

    if os.name == "nt":
        environment_python = project_root / ".venv-win/Scripts/python.exe"
    else:
        environment_python = project_root / ".venv-linux/bin/python"
    environment_python.parent.mkdir(parents=True)
    environment_python.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-m" ]]; then exit 86; fi\n'
        f'exec "{Path(sys.executable).as_posix()}" "$@"\n',
        encoding="utf-8",
    )
    environment_python.chmod(0o755)

    result = subprocess.run(
        [_find_bash(), "scripts/setup_python_env.sh"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.returncode != 86, (
        "script reached pip instead of rejecting the reused interpreter"
    )
    assert "--recreate" in result.stderr


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


def test_dockerignore_recursively_excludes_env_files_but_keeps_examples() -> None:
    dockerignore = _required_file(".dockerignore")
    rules = dockerignore.read_text(encoding="utf-8").splitlines()

    assert {"**/.env", "**/.env.*", "!**/*.example"} <= set(rules)

    spec = GitIgnoreSpec.from_lines(rules)
    ignored = [".env", ".env.prod", "docker/.env", "deploy/prod/.env.secret"]
    allowed = [
        ".env.example",
        "docker/.env.prod.example",
        "deploy/prod/.env.secret.example",
    ]
    assert all(spec.match_file(path) for path in ignored)
    assert all(not spec.match_file(path) for path in allowed)


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
