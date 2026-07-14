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
        project / "docker/.env.example",
        """NEXUSKERNEL_DEFAULT_PORT=8030
NEXUSKERNEL_API_KEY=
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
        project / "docker/other.example",
        "NexusKernel Starter nexuskernel-starter NEXUSKERNEL 8030\n",
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
        project / "docs/superpowers/plans/review.md",
        "NexusKernel Starter nexuskernel-starter NEXUSKERNEL 8030\n",
    )
    _write(
        project / "docs/superpowers/specs/review.md",
        "NexusKernel Starter nexuskernel-starter NEXUSKERNEL 8030\n",
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


def _replace_in_template_files(project: Path, old: str, new: str) -> None:
    relative_paths = (
        "README.md",
        "pyproject.toml",
        "config/settings.yaml",
        "docker/Dockerfile",
        "docker/docker-compose-dev.yml",
        "docker/.env.example",
        "docker/.env.dev.example",
        "docker/README.md",
        ".gitlab-ci.yml",
        "docs/guide/nested.md",
    )
    for relative_path in relative_paths:
        path = project / relative_path
        content = path.read_text(encoding="utf-8")
        path.write_text(content.replace(old, new), encoding="utf-8", newline="")


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
    generated_base_env = (template / "docker/.env").read_text(encoding="utf-8")
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
    assert "ACME_AI_DEFAULT_PORT=9123" in generated_base_env
    assert "ACME_AI_API_KEY=" in generated_base_env
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
    "separator",
    [
        "\n",
        "\r",
        "\r\n",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    ],
)
def test_name_rejects_every_splitlines_separator(
    template: Path, separator: str
) -> None:
    arguments = (
        "--name",
        f"Left{separator}Right",
        *VALID_ARGUMENTS[2:],
    )
    before = _snapshot(template)

    result = _run_initializer(template, arguments)

    assert result.returncode != 0
    assert "name" in result.stderr.lower()
    assert _snapshot(template) == before


def test_inserted_name_is_not_reprocessed_as_an_env_prefix(template: Path) -> None:
    arguments = (
        "--name",
        "NEXUSKERNEL Console",
        "--slug",
        "acme-console",
        "--env-prefix",
        "ACME",
        "--port",
        "9123",
    )

    result = _run_initializer(template, arguments)

    assert result.returncode == 0, result.stderr
    assert "# NEXUSKERNEL Console" in (template / "README.md").read_text(
        encoding="utf-8"
    )
    marker = json.loads(
        (template / ".nexuskernel-initialized").read_text(encoding="utf-8")
    )
    assert marker["name"] == "NEXUSKERNEL Console"


@pytest.mark.parametrize(
    "name",
    ["Acme Service-2.0 (Core)", "龙芯 服务_2.0"],
)
def test_safe_english_and_chinese_names_initialize(template: Path, name: str) -> None:
    arguments = ("--name", f"  {name}  ", *VALID_ARGUMENTS[2:])

    result = _run_initializer(template, arguments)

    assert result.returncode == 0, result.stderr
    marker = json.loads(
        (template / ".nexuskernel-initialized").read_text(encoding="utf-8")
    )
    assert marker["name"] == name


@pytest.mark.parametrize(
    "name",
    [
        "Acme: Service",
        "Acme # Service",
        'Acme "Service"',
        "Acme 'Service'",
        "Acme \\ Service",
        "Acme [Service]",
        "Acme {Service}",
        "Acme = Service",
        "Acme, Service",
        "x" * 81,
    ],
)
def test_unsafe_or_overlong_names_are_rejected_without_writes(
    template: Path, name: str
) -> None:
    arguments = ("--name", name, *VALID_ARGUMENTS[2:])
    before = _snapshot(template)

    result = _run_initializer(template, arguments)

    assert result.returncode != 0
    assert "name" in result.stderr.lower()
    assert _snapshot(template) == before


@pytest.mark.parametrize(
    ("token", "replacement"),
    [
        ("NexusKernel Starter", "Alternate Starter"),
        ("nexuskernel-starter", "alternate-starter"),
        ("NEXUSKERNEL", "ALTERNATE"),
        ("8030", "8031"),
    ],
    ids=("display-name", "slug", "env-prefix", "port"),
)
def test_missing_default_token_is_rejected_before_writes(
    template: Path, token: str, replacement: str
) -> None:
    _replace_in_template_files(template, token, replacement)
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert "template" in result.stderr.lower()
    assert _snapshot(template) == before
    assert not (template / ".nexuskernel-initialized").exists()


@pytest.mark.parametrize("port", [1, 65535])
def test_port_boundaries_initialize_successfully(template: Path, port: int) -> None:
    arguments = (*VALID_ARGUMENTS[:-1], str(port))

    result = _run_initializer(template, arguments)

    assert result.returncode == 0, result.stderr
    marker = json.loads(
        (template / ".nexuskernel-initialized").read_text(encoding="utf-8")
    )
    assert marker["port"] == port
    assert f"  port: {port}" in (template / "config/settings.yaml").read_text(
        encoding="utf-8"
    )


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


@pytest.mark.parametrize(
    ("full_option", "abbreviated_option"),
    [
        ("--name", "--na"),
        ("--slug", "--slu"),
        ("--env-prefix", "--env-p"),
        ("--port", "--po"),
    ],
)
def test_abbreviated_options_are_rejected_without_writes(
    template: Path, full_option: str, abbreviated_option: str
) -> None:
    arguments = tuple(
        abbreviated_option if item == full_option else item for item in VALID_ARGUMENTS
    )
    before = _snapshot(template)

    result = _run_initializer(template, arguments)

    assert result.returncode != 0
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


def test_existing_lock_is_preserved_and_rejected_without_writes(template: Path) -> None:
    lock = template / ".nexuskernel-init.lock"
    lock.write_text("external-owner\n", encoding="utf-8")
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert "lock" in result.stderr.lower()
    assert _snapshot(template) == before
    assert lock.read_text(encoding="utf-8") == "external-owner\n"


def test_lock_contains_pid_uses_private_mode_and_is_removed(
    template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_initializer(template)
    real_open = os.open
    real_plan = module._plan_writes
    observed: dict[str, object] = {}

    def tracking_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(path).name == ".nexuskernel-init.lock":
            observed["flags"] = flags
            observed["mode"] = mode
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def inspect_plan(root: Path, identity: object) -> object:
        lock = root / ".nexuskernel-init.lock"
        observed["pid"] = lock.read_text(encoding="ascii").strip()
        return real_plan(root, identity)

    monkeypatch.setattr(module.os, "open", tracking_open)
    monkeypatch.setattr(module, "_plan_writes", inspect_plan)
    identity = module.ProjectIdentity(
        name="Acme Intelligence",
        slug="acme-intelligence",
        env_prefix="ACME_AI",
        port=9123,
    )

    module.initialize_project(template, identity)

    assert observed["flags"] & os.O_CREAT
    assert observed["flags"] & os.O_EXCL
    assert observed["mode"] == 0o600
    assert observed["pid"] == str(os.getpid())
    assert not (template / ".nexuskernel-init.lock").exists()


@pytest.mark.parametrize("real_env", ["docker/.env", "docker/.env.dev"])
def test_existing_real_env_is_rejected_without_any_write(
    template: Path, real_env: str
) -> None:
    _write(template / real_env, "EXISTING=value\n")
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert real_env in result.stderr.replace("\\", "/")
    assert _snapshot(template) == before
    assert not (template / ".nexuskernel-initialized").exists()


@pytest.mark.parametrize(
    ("race_target", "external_content"),
    [
        ("docker/.env", "EXTERNAL_ENV=value\n"),
        (".nexuskernel-initialized", "external marker\n"),
    ],
)
def test_generated_target_race_preserves_external_target_and_rolls_back(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    race_target: str,
    external_content: str,
) -> None:
    module = _load_initializer(template)
    before = _snapshot(template)
    real_link = os.link
    target = template / race_target

    def racing_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        if Path(destination) == target:
            target.write_bytes(external_content.encode())
        real_link(source, destination)

    monkeypatch.setattr(module.os, "link", racing_link)
    identity = module.ProjectIdentity(
        name="Acme Intelligence",
        slug="acme-intelligence",
        env_prefix="ACME_AI",
        port=9123,
    )

    with pytest.raises(FileExistsError):
        module.initialize_project(template, identity)

    after = _snapshot(template)
    assert after.pop(race_target)[0] == external_content.encode()
    assert after == before
    assert not (template / ".nexuskernel-init.lock").exists()


def test_replace_failure_before_call_does_not_restore_attempted_target(
    template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_initializer(template)
    readme = template / "README.md"
    fixed_timestamp = 946684800_000_000_000
    os.utime(readme, ns=(fixed_timestamp, fixed_timestamp))
    before_content = readme.read_bytes()
    before_mode = stat.S_IMODE(readme.stat().st_mode)
    real_replace = os.replace

    def fail_before_replace(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        if Path(target) == readme:
            raise OSError("injected pre-replace failure")
        real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_before_replace)
    identity = module.ProjectIdentity(
        name="Acme Intelligence",
        slug="acme-intelligence",
        env_prefix="ACME_AI",
        port=9123,
    )

    with pytest.raises(OSError, match="injected pre-replace failure"):
        module.initialize_project(template, identity)

    assert readme.read_bytes() == before_content
    assert stat.S_IMODE(readme.stat().st_mode) == before_mode
    assert readme.stat().st_mtime_ns == fixed_timestamp
    assert not (template / ".nexuskernel-init.lock").exists()


def test_replace_failure_restores_files_env_and_marker(
    template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_initializer(template)
    before = _snapshot(template)
    real_replace = os.replace

    def fail_marker_replace(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        if Path(target).name == "README.md":
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
    assert not (template / "docker/.env").exists()
    assert not (template / "docker/.env.dev").exists()
    assert not (template / ".nexuskernel-initialized").exists()
    assert not list(template.rglob("*.tmp"))
    assert not (template / ".nexuskernel-init.lock").exists()


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


def test_generated_env_and_marker_are_planned_with_private_modes(
    template: Path,
) -> None:
    module = _load_initializer(template)
    identity = module.ProjectIdentity(
        name="Acme Intelligence",
        slug="acme-intelligence",
        env_prefix="ACME_AI",
        port=9123,
    )

    writes = module._plan_writes(template, identity)
    modes = {
        write.target.relative_to(template).as_posix(): write.mode for write in writes
    }

    assert modes["docker/.env"] == 0o600
    assert modes["docker/.env.dev"] == 0o600
    assert modes[".nexuskernel-initialized"] == 0o600


def test_non_whitelisted_files_are_never_modified(template: Path) -> None:
    protected = {
        path: path.read_bytes()
        for path in (
            template / "scripts/untouched.py",
            template / "tests/untouched.md",
            template / "outside-whitelist.txt",
            template / "docker/other.example",
            template / "docs/superpowers/plans/review.md",
            template / "docs/superpowers/specs/review.md",
        )
    }

    result = _run_initializer(template)

    assert result.returncode == 0, result.stderr
    assert {path: path.read_bytes() for path in protected} == protected
