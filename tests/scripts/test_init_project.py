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
DELIVERY_CONTRACT = ROOT / "scripts/delivery_contract.py"
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
        project / "AGENTS.md",
        "NexusKernel Starter uses nexus-kernel-starter. NEXUS_KERNEL_DEFAULT_PORT=8030\n",
    )
    _write(
        project / "README.md",
        """# NexusKernel Starter

Distribution: nexus-kernel-starter
Environment: NEXUS_KERNEL_API_KEY
Package: nexus_kernel
NEXUS_KERNEL_DEFAULT_PORT=8030
Historical example: 8030
CLI example: --port 8030
Local URL: http://127.0.0.1:8030
Unrelated assignment: OTHER_DEFAULT_PORT=8030
Embedded display text: BeforeNexusKernel StarterAfter
""",
    )
    _write(
        project / "pyproject.toml",
        """[project]
name = "nexus-kernel-starter"

[tool.setuptools.packages.find]
include = ["nexus_kernel*"]
""",
    )
    _write(
        project / "config/settings.yaml",
        """service:
  name: NexusKernel Starter
  env_prefix: NEXUS_KERNEL
  port: 8030
  sample_year: 8030
""",
    )
    _write(
        project / "docker/Dockerfile",
        "ENV NEXUS_KERNEL_DEFAULT_PORT=8030\nEXPOSE 8030\n",
    )
    _write(
        project / "docker/docker-compose-dev.yml",
        """services:
  api:
    ports:
      - "${NEXUS_KERNEL_PORT:-8030}:${NEXUS_KERNEL_PORT:-8030}"
      - "${OTHER_PORT:-8030}:8030"
    labels:
      sample-port: "8030"
""",
    )
    env_example = """NEXUS_KERNEL_DEFAULT_PORT=8030
NEXUS_KERNEL_PORT=8030
NEXUS_KERNEL_API_KEY=
DATABASE_URL=${DATABASE_URL:-}
HOST=localhost
"""
    for profile in ("dev", "preprod", "prod"):
        _write(project / f"docker/.env.{profile}.example", env_example)
    _write(
        project / "docker/other.example",
        "NexusKernel Starter nexus-kernel-starter NEXUS_KERNEL 8030\n",
    )
    _write(
        project / "docker/README.md",
        "Run NexusKernel Starter with NEXUS_KERNEL_DEFAULT_PORT=8030.\n",
    )
    _write(
        project / ".gitlab-ci.yml",
        """variables:
  NEXUS_KERNEL_DEFAULT_PORT: "8030"

container-smoke:
  script:
    - docker run -e NEXUS_KERNEL_PORT="$NEXUS_KERNEL_DEFAULT_PORT" -p "$NEXUS_KERNEL_DEFAULT_PORT:$NEXUS_KERNEL_DEFAULT_PORT" image
    - curl "http://docker:$NEXUS_KERNEL_DEFAULT_PORT/health/live"
    - docker run --name nexus-kernel-starter-default-api image
    - docker run --name nexus-kernel-starter-default-worker image
    - docker run --name nexus-kernel-starter-full-api image
    - docker run --name nexus-kernel-starter-full-worker image
""",
    )
    _write(
        project / "docs/guide/nested.md",
        "NexusKernel Starter uses nexus-kernel-starter and NEXUS_KERNEL.\n",
    )
    _write(
        project / ".codex/skills/group/sample/SKILL.md",
        "NexusKernel Starter uses nexus-kernel-starter. NEXUS_KERNEL_DEFAULT_PORT=8030\n",
    )
    _write(
        project / ".codex/skills/group/sample/agents/openai.yaml",
        "default_prompt: NexusKernel Starter NEXUS_KERNEL 8030\n",
    )
    _write(
        project / "docs/superpowers/plans/review.md",
        "NexusKernel Starter nexus-kernel-starter NEXUS_KERNEL 8030\n",
    )
    _write(
        project / "docs/superpowers/specs/review.md",
        "NexusKernel Starter nexus-kernel-starter NEXUS_KERNEL 8030\n",
    )
    _write(
        project / "scripts/untouched.py",
        'NAME = "NexusKernel Starter"\nPORT = 8030\n',
    )
    _write(
        project / "tests/untouched.md",
        "NexusKernel Starter nexus-kernel-starter NEXUS_KERNEL 8030\n",
    )
    _write(
        project / "outside-whitelist.txt",
        "NexusKernel Starter nexus-kernel-starter NEXUS_KERNEL 8030\n",
    )
    binary_doc = project / "docs/binary.md"
    binary_doc.parent.mkdir(parents=True, exist_ok=True)
    binary_doc.write_bytes(b"\xff\x00NexusKernel Starter\x80")
    return project


def _copy_initializer(project: Path) -> None:
    if not all(
        path.is_file()
        for path in (PYTHON_INITIALIZER, BASH_INITIALIZER, DELIVERY_CONTRACT)
    ):
        pytest.skip("initializer entry points are absent during the expected RED run")
    scripts = project / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PYTHON_INITIALIZER, scripts / PYTHON_INITIALIZER.name)
    shutil.copy2(BASH_INITIALIZER, scripts / BASH_INITIALIZER.name)
    shutil.copy2(DELIVERY_CONTRACT, scripts / DELIVERY_CONTRACT.name)


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
        "AGENTS.md",
        "pyproject.toml",
        "config/settings.yaml",
        "docker/Dockerfile",
        "docker/docker-compose-dev.yml",
        "docker/.env.dev.example",
        "docker/.env.preprod.example",
        "docker/.env.prod.example",
        "docker/README.md",
        ".gitlab-ci.yml",
        "docs/guide/nested.md",
        ".codex/skills/group/sample/SKILL.md",
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
    assert DELIVERY_CONTRACT.is_file(), "missing scripts/delivery_contract.py"
    assert (ROOT / "scripts/check_python_version.py").is_file()
    assert (ROOT / "scripts/check_text_contract.py").is_file()


def test_bash_entry_point_is_strict_and_only_delegates_to_python() -> None:
    assert BASH_INITIALIZER.is_file(), "missing scripts/init_project.sh"
    source = BASH_INITIALIZER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert all(
        candidate in source
        for candidate in ("python3", "python", "python3.13", "python3.12", "python3.11")
    )
    assert "${PYTHON:-}" in source
    assert "check_python_version.py" in source
    assert 'PATH="/usr/bin:/bin${PATH:+:$PATH}"' in source
    assert "init_project.py" in source
    assert '"$@"' in source
    assert "dirname" not in source
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
    dockerfile = (template / "docker/Dockerfile").read_text(encoding="utf-8")
    generated_envs = {
        profile: (template / f"docker/.env.{profile}").read_text(encoding="utf-8")
        for profile in ("dev", "preprod", "prod")
    }
    nested_doc = (template / "docs/guide/nested.md").read_text(encoding="utf-8")
    agents = (template / "AGENTS.md").read_text(encoding="utf-8")
    skill = (template / ".codex/skills/group/sample/SKILL.md").read_text(
        encoding="utf-8"
    )
    skill_metadata = (
        template / ".codex/skills/group/sample/agents/openai.yaml"
    ).read_text(encoding="utf-8")
    pipeline = (template / ".gitlab-ci.yml").read_text(encoding="utf-8")
    marker = json.loads(
        (template / ".nexus-kernel-initialized").read_text(encoding="utf-8")
    )

    assert "# Acme Intelligence" in readme
    assert "acme-intelligence" in readme and "ACME_AI_API_KEY" in readme
    assert "NEXUS_KERNEL_DEFAULT_PORT=8030" not in readme
    assert "ACME_AI_DEFAULT_PORT=9123" in readme
    assert "Historical example: 8030" in readme
    assert "CLI example: --port 9123" in readme
    assert "Local URL: http://127.0.0.1:9123" in readme
    assert "Unrelated assignment: OTHER_DEFAULT_PORT=8030" in readme
    assert "Embedded display text: BeforeNexusKernel StarterAfter" in readme
    assert 'name = "acme-intelligence"' in pyproject
    assert 'include = ["nexus_kernel*"]' in pyproject
    assert "  port: 9123" in config
    assert "  sample_year: 8030" in config
    assert "${ACME_AI_PORT:-9123}:${ACME_AI_PORT:-9123}" in compose
    assert "${OTHER_PORT:-8030}:8030" in compose
    assert 'sample-port: "8030"' in compose
    assert "EXPOSE 9123" in dockerfile
    for generated_env in generated_envs.values():
        assert "ACME_AI_DEFAULT_PORT=9123" in generated_env
        assert "ACME_AI_PORT=9123" in generated_env
        assert "ACME_AI_API_KEY=" in generated_env
    assert "Acme Intelligence" in nested_doc
    for transformed_guidance in (agents, skill):
        assert "Acme Intelligence" in transformed_guidance
        assert "acme-intelligence" in transformed_guidance
        assert "ACME_AI" in transformed_guidance
        assert "9123" in transformed_guidance
        assert "NexusKernel Starter" not in transformed_guidance
        assert "nexus-kernel-starter" not in transformed_guidance
        assert "NEXUS_KERNEL" not in transformed_guidance
        assert "8030" not in transformed_guidance
    assert skill_metadata == "default_prompt: NexusKernel Starter NEXUS_KERNEL 8030\n"
    assert 'ACME_AI_DEFAULT_PORT: "9123"' in pipeline
    assert "$ACME_AI_DEFAULT_PORT" in pipeline
    for role in (
        "default-api",
        "default-worker",
        "full-api",
        "full-worker",
    ):
        assert f"acme-intelligence-{role}" in pipeline
    assert "nexus-kernel-starter" not in pipeline
    assert "NEXUS_KERNEL" not in pipeline
    assert "8030" not in pipeline
    assert "nexus_kernel" in readme
    assert (template / "docs/binary.md").read_bytes().startswith(b"\xff\x00")
    assert marker == {
        "name": "Acme Intelligence",
        "slug": "acme-intelligence",
        "env_prefix": "ACME_AI",
        "port": 9123,
    }


@pytest.mark.parametrize("profile", ["dev", "preprod", "prod"])
def test_missing_required_environment_example_is_rejected_before_writes(
    template: Path, profile: str
) -> None:
    (template / f"docker/.env.{profile}.example").unlink()
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert f".env.{profile}.example" in result.stderr
    assert _snapshot(template) == before
    assert not (template / ".nexus-kernel-initialized").exists()


def test_missing_docker_directory_is_rejected_before_writes(template: Path) -> None:
    shutil.rmtree(template / "docker")
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert "docker" in result.stderr
    assert _snapshot(template) == before
    assert not (template / ".nexus-kernel-initialized").exists()


@pytest.mark.parametrize("kind", ["directory", "fifo", "symlink"])
def test_required_environment_example_must_be_a_regular_file(
    template: Path, kind: str
) -> None:
    example = template / "docker/.env.dev.example"
    example.unlink()
    if kind == "directory":
        example.mkdir()
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO is unavailable")
        os.mkfifo(example)
    else:
        target = template / "docker/env-example-target"
        _write(target, "NEXUS_KERNEL_API_KEY=\n")
        try:
            example.symlink_to(target)
        except OSError:
            pytest.skip("symbolic links are unavailable")
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert ".env.dev.example" in result.stderr
    assert _snapshot(template) == before
    assert not (template / ".nexus-kernel-initialized").exists()


def test_required_environment_example_must_be_utf8(template: Path) -> None:
    example = template / "docker/.env.dev.example"
    example.write_bytes(b"not-utf8-\xff")
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert "environment example must be UTF-8 text" in result.stderr
    assert _snapshot(template) == before
    assert not (template / ".nexus-kernel-initialized").exists()


@pytest.mark.parametrize("name", [".env.example", ".env.local.example"])
def test_unsupported_environment_example_is_rejected_before_writes(
    template: Path, name: str
) -> None:
    _write(template / "docker" / name, "NEXUS_KERNEL_API_KEY=\n")
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert name in result.stderr
    assert _snapshot(template) == before
    assert not (template / ".nexus-kernel-initialized").exists()


@pytest.mark.parametrize("profile_error", ["missing", "unknown"])
def test_environment_profile_preflight_runs_before_lock_creation(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_error: str,
) -> None:
    module = _load_initializer(template)
    if profile_error == "missing":
        (template / "docker/.env.dev.example").unlink()
    else:
        _write(template / "docker/.env.staging.example", "API_KEY=\n")
    lock_attempted = False

    def fail_if_locked(root: Path) -> Path:
        nonlocal lock_attempted
        lock_attempted = True
        raise AssertionError(f"profile preflight wrote lock under {root}")

    monkeypatch.setattr(module, "_acquire_lock", fail_if_locked)
    identity = module.ProjectIdentity(
        name="Acme Intelligence",
        slug="acme-intelligence",
        env_prefix="ACME_AI",
        port=9123,
    )

    with pytest.raises(module.InitializationError):
        module.initialize_project(template, identity)

    assert not lock_attempted
    assert not (template / ".nexus-kernel-init.lock").exists()


def test_environment_profiles_are_rechecked_while_lock_is_held(
    template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_initializer(template)
    real_environment_examples = module._environment_examples
    calls = 0

    def changing_environment_examples(root: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise module.InitializationError("environment profiles changed")
        return real_environment_examples(root)

    monkeypatch.setattr(module, "_environment_examples", changing_environment_examples)
    identity = module.ProjectIdentity(
        name="Acme Intelligence",
        slug="acme-intelligence",
        env_prefix="ACME_AI",
        port=9123,
    )

    with pytest.raises(module.InitializationError, match="profiles changed"):
        module.initialize_project(template, identity)

    assert calls == 2
    assert not (template / ".nexus-kernel-init.lock").exists()
    assert not (template / ".nexus-kernel-initialized").exists()


@pytest.mark.parametrize("directory", ["config", "docs"])
def test_nested_whitelist_symlink_directory_is_rejected(
    template: Path, tmp_path: Path, directory: str
) -> None:
    outside = tmp_path / f"outside-{directory}"
    outside.mkdir()
    suffix = ".yaml" if directory == "config" else ".md"
    _write(outside / f"hidden{suffix}", "NexusKernel Starter NEXUS_KERNEL 8030\n")
    linked = template / directory / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    result = _run_initializer(template)

    assert result.returncode != 0
    assert "symlinked directory" in result.stderr
    assert not (template / ".nexus-kernel-initialized").exists()


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
        "NEXUS_KERNEL Console",
        "--slug",
        "acme-console",
        "--env-prefix",
        "ACME",
        "--port",
        "9123",
    )

    result = _run_initializer(template, arguments)

    assert result.returncode == 0, result.stderr
    assert "# NEXUS_KERNEL Console" in (template / "README.md").read_text(
        encoding="utf-8"
    )
    marker = json.loads(
        (template / ".nexus-kernel-initialized").read_text(encoding="utf-8")
    )
    assert marker["name"] == "NEXUS_KERNEL Console"


@pytest.mark.parametrize(
    "name",
    ["Acme 123", "龙芯 服务_2.0", "True Service"],
)
def test_safe_english_and_chinese_names_initialize(template: Path, name: str) -> None:
    arguments = ("--name", f"  {name}  ", *VALID_ARGUMENTS[2:])

    result = _run_initializer(template, arguments)

    assert result.returncode == 0, result.stderr
    marker = json.loads(
        (template / ".nexus-kernel-initialized").read_text(encoding="utf-8")
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
    "name",
    ["-", "1234", "1.0", "2026-07-15", "0xFF", "0xCAFE", "0b1010"],
)
def test_names_without_unicode_letters_are_rejected_without_writes(
    template: Path, name: str
) -> None:
    arguments = ("--name", name, *VALID_ARGUMENTS[2:])
    before = _snapshot(template)

    result = _run_initializer(template, arguments)

    assert result.returncode != 0
    assert "name" in result.stderr.lower()
    assert _snapshot(template) == before


@pytest.mark.parametrize(
    "name",
    [
        "null",
        "~",
        "TRUE",
        "False",
        "yes",
        "NO",
        "on",
        "OFF",
        "y",
        "N",
        ".inf",
        "-.inf",
        "+.inf",
        ".nan",
    ],
)
def test_yaml_implicit_scalar_names_are_rejected_after_trim_and_casefold(
    template: Path, name: str
) -> None:
    arguments = ("--name", f"  {name}  ", *VALID_ARGUMENTS[2:])
    before = _snapshot(template)

    result = _run_initializer(template, arguments)

    assert result.returncode != 0
    assert "name" in result.stderr.lower()
    assert _snapshot(template) == before


@pytest.mark.parametrize(
    ("token", "replacement"),
    [
        ("NexusKernel Starter", "Alternate Starter"),
        ("nexus-kernel-starter", "alternate-starter"),
        ("NEXUS_KERNEL", "ALTERNATE"),
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
    assert not (template / ".nexus-kernel-initialized").exists()


@pytest.mark.parametrize("port", [1, 65535])
def test_port_boundaries_initialize_successfully(template: Path, port: int) -> None:
    arguments = (*VALID_ARGUMENTS[:-1], str(port))

    result = _run_initializer(template, arguments)

    assert result.returncode == 0, result.stderr
    marker = json.loads(
        (template / ".nexus-kernel-initialized").read_text(encoding="utf-8")
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


@pytest.mark.parametrize("missing", ["README.md", "pyproject.toml", "AGENTS.md"])
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
    lock = template / ".nexus-kernel-init.lock"
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
        if Path(path).name == ".nexus-kernel-init.lock":
            observed["flags"] = flags
            observed["mode"] = mode
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def inspect_plan(root: Path, identity: object) -> object:
        lock = root / ".nexus-kernel-init.lock"
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
    assert not (template / ".nexus-kernel-init.lock").exists()


@pytest.mark.parametrize(
    "real_env",
    ["docker/.env.dev", "docker/.env.preprod", "docker/.env.prod"],
)
def test_existing_real_env_is_rejected_without_any_write(
    template: Path, real_env: str
) -> None:
    _write(template / real_env, "EXISTING=value\n")
    before = _snapshot(template)

    result = _run_initializer(template)

    assert result.returncode != 0
    assert real_env in result.stderr.replace("\\", "/")
    assert _snapshot(template) == before
    assert not (template / ".nexus-kernel-initialized").exists()


@pytest.mark.parametrize(
    ("race_target", "external_content"),
    [
        ("docker/.env.dev", "EXTERNAL_ENV=value\n"),
        (".nexus-kernel-initialized", "external marker\n"),
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
    assert not (template / ".nexus-kernel-init.lock").exists()


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
    assert not (template / ".nexus-kernel-init.lock").exists()


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
    for profile in ("dev", "preprod", "prod"):
        assert not (template / f"docker/.env.{profile}").exists()
    assert not (template / ".nexus-kernel-initialized").exists()
    assert not list(template.rglob("*.tmp"))
    assert not (template / ".nexus-kernel-init.lock").exists()


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

    for profile in ("dev", "preprod", "prod"):
        assert modes[f"docker/.env.{profile}"] == 0o600
    assert modes[".nexus-kernel-initialized"] == 0o600


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
            template / ".codex/skills/group/sample/agents/openai.yaml",
        )
    }

    result = _run_initializer(template)

    assert result.returncode == 0, result.stderr
    assert {path: path.read_bytes() for path in protected} == protected
