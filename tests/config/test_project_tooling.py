from __future__ import annotations

import ast
import os
import platform
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
    "starlette==1.3.1",
    "jsonschema==4.26.0",
]

OPTIONAL_DEPENDENCIES = {
    "ai": [
        "google-adk==2.4.0",
        "google-genai==2.12.1",
        "litellm==1.87.0",
        "openai==2.40.0",
    ],
    "mongo": ["pymongo==4.17.0"],
    "redis": ["redis==8.0.1"],
    "qdrant": ["qdrant-client==1.18.0"],
    "otel": [
        "opentelemetry-api==1.41.1",
        "opentelemetry-sdk==1.41.1",
        "opentelemetry-exporter-otlp-proto-grpc==1.41.1",
    ],
    "postgres": ["asyncpg==0.31.0"],
    "dev": [
        "build==1.5.0",
        "setuptools==83.0.0",
        "packaging==26.2",
        "pytest==9.1.1",
        "pytest-asyncio==1.4.0",
        "httpx2==2.7.0",
        "ruff==0.15.21",
        "mypy==2.2.0",
        "pathspec==1.1.1",
        "types-PyYAML==6.0.12.20260518",
        "uv==0.11.29",
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


def _find_powershell() -> str:
    for candidate in (shutil.which("pwsh"), shutil.which("powershell")):
        if candidate:
            return candidate
    pytest.skip("PowerShell is required to verify scripts/setup_python_env.ps1")


def _required_file(relative_path: str) -> Path:
    path = ROOT / relative_path
    assert path.is_file(), f"missing required project file: {relative_path}"
    return path


def _load_pyproject() -> dict[str, object]:
    with _required_file("pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def _project_distribution() -> str:
    distribution = _load_pyproject()["project"]["name"]
    assert isinstance(distribution, str)
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", distribution)
    return distribution


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
    constraints_dir = project_root / "constraints"
    script_dir.mkdir(parents=True)
    package_dir.mkdir()
    constraints_dir.mkdir()
    shutil.copy2(_required_file("scripts/setup_python_env.sh"), script_dir)
    shutil.copy2(_required_file("scripts/check_python_version.py"), script_dir)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (constraints_dir / "py311-313.txt").write_text(
        "# Test fixture has no dependencies.\n", encoding="utf-8"
    )
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
requires-python = ">=3.11,<3.14"

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

    environment_dir = project_root / (
        ".venv-macos" if platform.system() == "Darwin" else ".venv-linux"
    )
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
    assert build_system["requires"] == ["setuptools==83.0.0"]

    project = config["project"]
    assert project["name"] == _project_distribution()
    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["dependencies"] == CORE_DEPENDENCIES
    assert "httpx==0.28.1" not in project["dependencies"]


def test_pyproject_defines_exact_optional_dependency_groups() -> None:
    project = _load_pyproject()["project"]

    assert project["optional-dependencies"] == OPTIONAL_DEPENDENCIES
    assert "httpx2==2.7.0" in project["optional-dependencies"]["dev"]


def test_universal_constraints_are_generated_from_pyproject() -> None:
    constraints = _required_file("constraints/py311-313.txt").read_text(
        encoding="utf-8"
    )
    script = _required_file("scripts/compile_constraints.sh").read_text(
        encoding="utf-8"
    )

    assert constraints.startswith(
        "# Generated by scripts/compile_constraints.sh; do not edit manually.\n"
        "# Source of truth: pyproject.toml\n"
    )
    assert "uv==0.11.29" in constraints
    assert all(
        argument in script
        for argument in (
            "--all-extras",
            "--universal",
            "--python-version 3.11",
            "--no-annotate",
            "--no-header",
            "--check",
        )
    )
    assert "uv 0.11.29" in script
    assert "constraint compilation is Linux-only" in script


def test_pyproject_discovers_packages_and_configures_tools() -> None:
    config = _load_pyproject()

    assert config["tool"]["setuptools"]["packages"]["find"]["include"] == [
        "nexus_kernel*",
        "app*",
    ]
    assert config["tool"]["pytest"]["ini_options"]["asyncio_mode"] == "auto"
    assert config["tool"]["ruff"]["target-version"] == "py311"
    assert config["tool"]["ruff"]["lint"]["extend-select"] == ["C90"]
    assert config["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"] == 18
    assert config["tool"]["mypy"]["files"] == ["app", "nexus_kernel"]


def test_import_package_roots_exist() -> None:
    assert _required_file("nexus_kernel/__init__.py")
    assert _required_file("app/__init__.py")


def test_repository_uses_ecosystem_specific_nexus_kernel_identifiers() -> None:
    project = _load_pyproject()["project"]
    tool_config = _load_pyproject()["tool"]

    assert project["name"] == _project_distribution()
    assert tool_config["setuptools"]["packages"]["find"]["include"] == [
        "nexus_kernel*",
        "app*",
    ]
    assert tool_config["mypy"]["files"] == ["app", "nexus_kernel"]
    legacy_package = "nexus" + "kernel"
    legacy_env_prefix = "NEXUS" + "KERNEL"
    assert _required_file("nexus_kernel/__init__.py")
    assert not (ROOT / legacy_package).exists()

    inventory = subprocess.run(
        ["git", "ls-files", "--cached"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    relative_paths = inventory.stdout.splitlines()
    tracked_paths = set(relative_paths)
    owned_sources = {
        path.relative_to(ROOT).as_posix()
        for root_name in ("app", "nexus_kernel", "scripts", "tests")
        for path in (ROOT / root_name).rglob("*")
        if path.is_file()
        and path.suffix.casefold() in {".py", ".sh", ".ps1", ".bat", ".cmd"}
        and not {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        }.intersection(path.parts)
    }
    required_migration_infrastructure = {
        "nexus_kernel/ai/adk_sessions.py",
        "scripts/__init__.py",
        "scripts/check_text_contract.py",
        "scripts/delivery_contract.py",
        "tests/__init__.py",
        "tests/ai/test_ai_contracts.py",
        "tests/project_contract.py",
    }
    assert required_migration_infrastructure <= owned_sources
    assert owned_sources <= tracked_paths, (
        "all source and test infrastructure must be tracked"
    )

    offenders: list[str] = []
    for relative_path in relative_paths:
        path = ROOT / relative_path
        if not path.is_file():
            continue
        if legacy_package in relative_path:
            offenders.append(relative_path)
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if legacy_package in content or legacy_env_prefix in content:
            offenders.append(relative_path)

    assert offenders == []


def test_all_test_modules_are_nonempty_parseable_and_keep_gateway_contracts() -> None:
    test_paths = sorted((ROOT / "tests").rglob("test_*.py"))
    assert test_paths
    parsed: dict[Path, ast.Module] = {}
    for path in test_paths:
        source = path.read_text(encoding="utf-8", errors="strict")
        assert source.strip(), f"empty test module: {path.relative_to(ROOT)}"
        parsed[path] = ast.parse(source, filename=path.relative_to(ROOT).as_posix())

    gateway_path = ROOT / "tests/gateway/test_app.py"
    definitions = {
        node.name
        for node in parsed[gateway_path].body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    required_gateway_contracts = {
        "test_request_body_limit_returns_safe_413_envelope",
        "test_streamed_request_body_is_limited_without_content_length",
        "test_declarative_route_timeout_returns_504",
        "test_response_buffer_limit_returns_safe_500",
        "test_background_task_runs_after_response_commit",
        "test_gateway_rejects_router_conflicts_with_builtin_and_other_routers",
        "test_gateway_rejects_semantically_duplicate_dynamic_paths",
        "test_gateway_rejects_nested_router_conflicts",
        "test_gateway_rejects_mount_conflicts",
    }
    assert required_gateway_contracts <= definitions


def test_tracked_text_contract_rejects_committed_whitespace_in_clean_checkout(
    tmp_path: Path,
) -> None:
    from scripts import check_text_contract

    (tmp_path / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    (tmp_path / "bad.txt").write_text("bad trailing space \n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=NexusKernel Test",
            "-c",
            "user.email=noreply@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert status.stdout == ""
    assert check_text_contract.check(tmp_path) == ["trailing whitespace: bad.txt:1"]


def test_tracked_text_contract_enforces_final_newline_and_declared_eol(
    tmp_path: Path,
) -> None:
    from scripts import check_text_contract

    (tmp_path / ".gitattributes").write_text(
        "* text=auto eol=lf\n*.ps1 text eol=crlf\n", encoding="utf-8"
    )
    (tmp_path / "missing.txt").write_text("missing", encoding="utf-8")
    (tmp_path / "script.ps1").write_bytes(b"Write-Output ok\r\n")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    raw_blob = subprocess.run(
        ["git", "hash-object", "-w", "--no-filters", "script.ps1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "update-index",
            "--cacheinfo",
            f"100644,{raw_blob},script.ps1",
        ],
        cwd=tmp_path,
        check=True,
    )

    assert check_text_contract.check(tmp_path) == [
        "missing final newline: missing.txt:1",
        "unexpected CRLF: script.ps1:1",
    ]


def test_setup_script_has_supported_python_environment_contract() -> None:
    script_path = _required_file("scripts/setup_python_env.sh")
    script = script_path.read_text(encoding="utf-8")
    bash = _find_bash()

    assert "set -euo pipefail" in script
    assert all(option in script for option in ("--python", "--recreate", "--help"))
    assert all(
        platform in script
        for platform in ("MINGW*", "MSYS*", "CYGWIN*", "Linux*", "Darwin*")
    )
    assert all(
        environment in script
        for environment in (".venv-win", ".venv-linux", ".venv-macos")
    )
    assert "check_python_version.py" in script
    assert '"$python_command" "$version_checker" --quiet' in script
    assert '"$environment_python" -m pip --version' in script
    linux_branch = script.split("Linux*)", 1)[1].split(";;", 1)[0]
    windows_branch = script.split("MINGW*|MSYS*|CYGWIN*)", 1)[1].split(";;", 1)[0]
    assert 'constraints_arguments=(--constraint "$constraints_file")' in linux_branch
    assert "constraints_arguments" not in windows_branch
    assert "constraints/py311-313.txt" in script
    assert '--editable ".[ai,dev]"' in script
    assert "rm -rf --" in script
    assert "$project_root/.venv-win" in script
    assert "$project_root/.venv-linux" in script
    assert "$project_root/.venv-macos" in script
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


def test_powershell_setup_script_has_equivalent_windows_contract() -> None:
    script = _required_file("scripts/setup_python_env.ps1").read_text(encoding="utf-8")

    assert all(option in script for option in ("Python", "Recreate", "Help"))
    assert ".venv-win" in script
    assert "check_python_version.py" in script
    assert '"--quiet"' in script
    assert "sys.prefix" in script
    assert "sys.base_prefix" in script
    assert '"pip", "--version"' in script
    assert '"pip", "install", "--upgrade", "pip"' not in script
    assert ".[ai,dev]" in script
    assert "Remove-Item" in script

    help_result = subprocess.run(
        [
            _find_powershell(),
            "-NoProfile",
            "-File",
            str(ROOT / "scripts/setup_python_env.ps1"),
            "-Help",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert all(
        option in help_result.stdout for option in ("-Python", "-Recreate", "-Help")
    )


@pytest.mark.skipif(os.name != "nt", reason="PowerShell setup targets Windows")
def test_powershell_setup_creates_reuses_and_recreates_environment(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "powershell-setup"
    _write_minimal_bootstrap_project(project_root)
    shutil.copy2(
        _required_file("scripts/setup_python_env.ps1"), project_root / "scripts"
    )
    command = [
        _find_powershell(),
        "-NoProfile",
        "-File",
        "scripts/setup_python_env.ps1",
        "-Python",
        sys.executable,
    ]
    environment = os.environ.copy()
    environment.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INDEX": "1"})

    first = subprocess.run(
        command,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    sentinel = project_root / ".venv-win" / "replace-on-recreate.txt"
    sentinel.write_text("sentinel", encoding="utf-8")
    reused = subprocess.run(
        command,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    recreated = subprocess.run(
        [*command, "-Recreate"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert reused.returncode == 0, reused.stderr
    assert recreated.returncode == 0, recreated.stderr
    assert not sentinel.exists()
    assert (project_root / ".venv-win" / "Scripts" / "python.exe").is_file()


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


@pytest.mark.skipif(os.name != "nt", reason="Git Bash PATH layout is Windows-specific")
def test_setup_script_reuses_existing_environment_without_global_python(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "reuse-without-global-python"
    _write_minimal_bootstrap_project(project_root)
    first_run = _run_setup(project_root)
    assert first_run.returncode == 0, first_run.stderr

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": "/usr/bin:/bin",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
        }
    )
    reused = subprocess.run(
        [_find_bash(), "scripts/setup_python_env.sh"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert reused.returncode == 0, reused.stderr
    assert "could not find Python" not in reused.stderr


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
    constraints_dir = project_root / "constraints"
    script_dir.mkdir(parents=True)
    constraints_dir.mkdir()
    shutil.copy2(_required_file("scripts/setup_python_env.sh"), script_dir)
    shutil.copy2(_required_file("scripts/check_python_version.py"), script_dir)
    (constraints_dir / "py311-313.txt").write_text(
        "# Test fixture has no dependencies.\n", encoding="utf-8"
    )

    _, scripts_python, bin_python = _environment_layouts(project_root)
    environment_python = scripts_python if os.name == "nt" else bin_python
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
        ".venv-macos/pyvenv.cfg",
        "app/__pycache__/module.cpython-311.pyc",
        ".pytest_cache/state",
        ".mypy_cache/state",
        ".ruff_cache/state",
        "build/artifact.whl",
        "dist/artifact.whl",
        "runtime.log",
        "runtime.out",
        "runtime.err",
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
        ".venv-macos",
        "**/__pycache__",
        "**/.pytest_cache",
        "**/.mypy_cache",
        "**/.ruff_cache",
        ".env",
        ".env.*",
        "!*.example",
        ".codex",
        "docs",
        "tests",
        "scripts",
        "AGENTS.md",
        ".gitlab-ci.yml",
        "*.out",
        "*.err",
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
        if not {".venv-win", ".venv-linux", ".venv-macos"}.intersection(
            path.relative_to(ROOT).parts
        )
    ]
    assert requirements_files == []
    assert not (ROOT / ".venv").exists()


def test_security_scan_excludes_virtual_environments_and_build_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import security_scan

    monkeypatch.setattr(security_scan, "ROOT", tmp_path)
    (tmp_path / ".dockerignore").write_text(
        ".env\n.env.*\n**/.env\n**/.env.*\n",
        encoding="utf-8",
    )
    for relative_path in (
        ".venv-win/package.py",
        ".venv-linux/package.py",
        ".venv-macos/package.py",
        "build/package.py",
        "dist/package.py",
        "sample.egg-info/metadata.py",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("API_KEY = 'sk-' + 'placeholder'\n", encoding="utf-8")

    assert security_scan.scan() == []
