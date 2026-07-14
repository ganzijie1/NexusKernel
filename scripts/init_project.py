#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Sequence


DEFAULT_NAME = "NexusKernel Starter"
DEFAULT_SLUG = "nexuskernel-starter"
DEFAULT_ENV_PREFIX = "NEXUSKERNEL"
DEFAULT_PORT = "8030"
MARKER_NAME = ".nexuskernel-initialized"
MAX_NAME_LENGTH = 100

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DEFAULT_NAME_RE = re.compile(
    rf"(?<![A-Za-z0-9_]){re.escape(DEFAULT_NAME)}(?![A-Za-z0-9_])"
)
_DEFAULT_SLUG_RE = re.compile(rf"(?<![a-z0-9-]){re.escape(DEFAULT_SLUG)}(?![a-z0-9-])")
_DEFAULT_ENV_PREFIX_RE = re.compile(
    rf"(?<![A-Z0-9_]){re.escape(DEFAULT_ENV_PREFIX)}(?=_|[^A-Z0-9_]|$)"
)
_DEFAULT_PORT_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?<![A-Z0-9_]){DEFAULT_ENV_PREFIX}_DEFAULT_PORT[ \t]*=[ \t]*)"
    rf"{DEFAULT_PORT}(?![0-9])"
)
_DEFAULT_PORT_YAML_KEY_RE = re.compile(
    rf"(?m)(?P<prefix>^[ \t]*{DEFAULT_ENV_PREFIX}_DEFAULT_PORT[ \t]*:[ \t]*[\"']?)"
    rf"{DEFAULT_PORT}(?P<suffix>[\"']?)(?![0-9])"
)
_PORT_YAML_RE = re.compile(
    rf"(?m)(?P<prefix>^[ \t]*port[ \t]*:[ \t]*[\"']?){DEFAULT_PORT}"
    r"(?P<suffix>[\"']?[ \t]*(?:#.*)?\r?$)"
)
_COMPOSE_DEFAULT_PORT_RE = re.compile(
    r"(?P<prefix>\$\{"
    + re.escape(DEFAULT_ENV_PREFIX)
    + r"_PORT:-)"
    + re.escape(DEFAULT_PORT)
    + r"(?P<suffix>\})"
)
_ENV_EXAMPLE_RE = re.compile(r"^\.env\.[A-Za-z0-9_-]+\.example$")


class InitializationError(RuntimeError):
    """Raised when initialization cannot safely proceed."""


@dataclass(frozen=True)
class ProjectIdentity:
    name: str
    slug: str
    env_prefix: str
    port: int


@dataclass(frozen=True)
class _PlannedWrite:
    target: Path
    content: bytes
    mode: int
    original_content: bytes | None
    original_mode: int | None


def _fail(message: str) -> NoReturn:
    raise InitializationError(message)


def _validate_identity(identity: ProjectIdentity) -> ProjectIdentity:
    name = identity.name.strip()
    if not name:
        _fail("name must not be empty")
    if "\n" in name or "\r" in name:
        _fail("name must be a single line")
    if len(name) > MAX_NAME_LENGTH:
        _fail(f"name must be at most {MAX_NAME_LENGTH} characters")
    if not _SLUG_RE.fullmatch(identity.slug):
        _fail(
            "slug must contain only lowercase letters and digits separated by "
            "single hyphens"
        )
    if not _ENV_PREFIX_RE.fullmatch(identity.env_prefix):
        _fail(
            "env-prefix must start with a letter and contain only uppercase "
            "letters, digits, and underscores"
        )
    if isinstance(identity.port, bool) or not 1 <= identity.port <= 65535:
        _fail("port must be an integer from 1 through 65535")
    return ProjectIdentity(
        name=name,
        slug=identity.slug,
        env_prefix=identity.env_prefix,
        port=identity.port,
    )


def _relative_display(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _require_safe_file(root: Path, path: Path, *, required: bool = False) -> bool:
    if not _path_exists(path):
        if required:
            _fail(f"required file is missing: {_relative_display(root, path)}")
        return False
    if path.is_symlink():
        _fail(f"refusing symlinked file: {_relative_display(root, path)}")
    if not path.is_file():
        if required:
            _fail(f"required path is not a file: {_relative_display(root, path)}")
        return False

    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        _fail(f"file escapes project root: {_relative_display(root, path)}")
    return True


def _matching_files(root: Path, directory: str, suffixes: set[str]) -> list[Path]:
    base = root / directory
    if not _path_exists(base):
        return []
    if base.is_symlink():
        _fail(f"refusing symlinked directory: {directory}")
    if not base.is_dir():
        return []

    matches: list[Path] = []
    for path in base.rglob("*"):
        if path.suffix not in suffixes:
            continue
        if path.is_symlink():
            _fail(f"refusing symlinked file: {_relative_display(root, path)}")
        if path.is_file():
            matches.append(path)
    return matches


def _discover_whitelist(root: Path) -> list[Path]:
    required = [root / "README.md", root / "pyproject.toml"]
    for path in required:
        _require_safe_file(root, path, required=True)

    candidates = required.copy()
    candidates.extend(_matching_files(root, "config", {".yaml", ".yml"}))

    docker = root / "docker"
    if _path_exists(docker):
        if docker.is_symlink():
            _fail("refusing symlinked directory: docker")
        if docker.is_dir():
            candidates.extend(
                [
                    docker / "Dockerfile",
                    docker / "README.md",
                    *docker.glob("docker-compose-*.yml"),
                    *docker.glob("*.example"),
                ]
            )

    candidates.append(root / ".gitlab-ci.yml")
    candidates.extend(_matching_files(root, "docs", {".md"}))

    safe_candidates: dict[str, Path] = {}
    for path in candidates:
        if _require_safe_file(root, path):
            safe_candidates[_relative_display(root, path)] = path
    return [safe_candidates[key] for key in sorted(safe_candidates)]


def _replace_port(text: str, relative_path: str, port: int) -> str:
    replacement = str(port)
    text = _DEFAULT_PORT_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{replacement}", text
    )
    text = _DEFAULT_PORT_YAML_KEY_RE.sub(
        lambda match: f"{match.group('prefix')}{replacement}{match.group('suffix')}",
        text,
    )

    if relative_path.endswith((".yaml", ".yml")):
        text = _PORT_YAML_RE.sub(
            lambda match: (
                f"{match.group('prefix')}{replacement}{match.group('suffix')}"
            ),
            text,
        )
    if relative_path.startswith("docker/docker-compose-") and relative_path.endswith(
        ".yml"
    ):
        text = _COMPOSE_DEFAULT_PORT_RE.sub(
            lambda match: (
                f"{match.group('prefix')}{replacement}{match.group('suffix')}"
            ),
            text,
        )
    return text


def _transform_text(text: str, relative_path: str, identity: ProjectIdentity) -> str:
    transformed = _replace_port(text, relative_path, identity.port)
    transformed = _DEFAULT_NAME_RE.sub(identity.name, transformed)
    transformed = _DEFAULT_SLUG_RE.sub(identity.slug, transformed)
    transformed = _DEFAULT_ENV_PREFIX_RE.sub(identity.env_prefix, transformed)
    return transformed


def _decode_candidate(
    root: Path, path: Path, content: bytes, *, required: bool
) -> str | None:
    if b"\x00" in content:
        if required:
            _fail(f"required file is binary: {_relative_display(root, path)}")
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        if required:
            _fail(f"required file is not UTF-8: {_relative_display(root, path)}")
        return None


def _real_env_path(example: Path) -> Path | None:
    if not _ENV_EXAMPLE_RE.fullmatch(example.name):
        return None
    return example.with_name(example.name.removesuffix(".example"))


def _plan_writes(root: Path, identity: ProjectIdentity) -> list[_PlannedWrite]:
    files = _discover_whitelist(root)
    originals: dict[Path, tuple[bytes, int]] = {}
    transformed: dict[Path, bytes] = {}

    for path in files:
        content = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
        originals[path] = (content, mode)
        text = _decode_candidate(
            root,
            path,
            content,
            required=path.name in {"README.md", "pyproject.toml"}
            and path.parent == root,
        )
        if text is None:
            continue
        transformed[path] = _transform_text(
            text, _relative_display(root, path), identity
        ).encode("utf-8")

    generated: list[tuple[Path, bytes, int]] = []
    for example in files:
        if example.parent != root / "docker":
            continue
        real_env = _real_env_path(example)
        if real_env is None:
            continue
        if _path_exists(real_env):
            _fail(
                "refusing to overwrite existing environment file: "
                f"{_relative_display(root, real_env)}"
            )
        example_content = transformed.get(example)
        if example_content is None:
            _fail(
                "environment example must be UTF-8 text: "
                f"{_relative_display(root, example)}"
            )
        generated.append((real_env, example_content, originals[example][1]))

    writes = [
        _PlannedWrite(
            target=path,
            content=content,
            mode=originals[path][1],
            original_content=originals[path][0],
            original_mode=originals[path][1],
        )
        for path, content in transformed.items()
        if content != originals[path][0]
    ]
    writes.extend(
        _PlannedWrite(
            target=target,
            content=content,
            mode=mode,
            original_content=None,
            original_mode=None,
        )
        for target, content, mode in generated
    )
    writes.sort(key=lambda write: _relative_display(root, write.target))

    marker_content = (
        json.dumps(
            {
                "name": identity.name,
                "slug": identity.slug,
                "env_prefix": identity.env_prefix,
                "port": identity.port,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    writes.append(
        _PlannedWrite(
            target=root / MARKER_NAME,
            content=marker_content,
            mode=0o644,
            original_content=None,
            original_mode=None,
        )
    )
    return writes


def _stage_write(write: _PlannedWrite) -> Path:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{write.target.name}.",
        suffix=".tmp",
        dir=write.target.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(write.content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, write.mode)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _restore_write(write: _PlannedWrite) -> None:
    if write.original_content is None:
        write.target.unlink(missing_ok=True)
        return
    write.target.write_bytes(write.original_content)
    if write.original_mode is not None:
        os.chmod(write.target, write.original_mode)


def _commit_writes(writes: list[_PlannedWrite]) -> None:
    staged: dict[Path, Path] = {}
    replaced: list[_PlannedWrite] = []
    try:
        for write in writes:
            staged[write.target] = _stage_write(write)
        for write in writes:
            replaced.append(write)
            os.replace(staged[write.target], write.target)
    except BaseException as error:
        rollback_errors: list[OSError] = []
        for write in reversed(replaced):
            try:
                _restore_write(write)
            except OSError as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            raise InitializationError(
                "initialization failed and rollback could not restore every file"
            ) from error
        raise
    finally:
        for temp_path in staged.values():
            temp_path.unlink(missing_ok=True)


def initialize_project(root: Path, identity: ProjectIdentity) -> None:
    """Initialize a template copy in place with atomic rollback on failure."""
    root = root.resolve(strict=True)
    marker = root / MARKER_NAME
    if _path_exists(marker):
        _fail(f"project is already initialized: {MARKER_NAME}")

    validated = _validate_identity(identity)
    writes = _plan_writes(root, validated)
    _commit_writes(writes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize this NexusKernel Starter repository once."
    )
    parser.add_argument("--name", required=True, help="project display name")
    parser.add_argument("--slug", required=True, help="Python distribution slug")
    parser.add_argument(
        "--env-prefix", required=True, help="uppercase environment-variable prefix"
    )
    parser.add_argument("--port", required=True, type=int, help="default service port")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    namespace = parser.parse_args(argv)
    identity = ProjectIdentity(
        name=namespace.name,
        slug=namespace.slug,
        env_prefix=namespace.env_prefix,
        port=namespace.port,
    )
    project_root = Path(__file__).resolve().parent.parent
    try:
        initialize_project(project_root, identity)
    except (InitializationError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
