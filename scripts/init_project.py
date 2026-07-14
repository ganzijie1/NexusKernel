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
LOCK_NAME = ".nexuskernel-init.lock"
MAX_NAME_LENGTH = 80

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DISPLAY_NAME_RE = re.compile(r"^[\w .()\-]+$")
_DEFAULT_NAME_RE = re.compile(
    rf"(?<![A-Za-z0-9_]){re.escape(DEFAULT_NAME)}(?![A-Za-z0-9_])"
)
_DEFAULT_SLUG_RE = re.compile(rf"(?<![a-z0-9-]){re.escape(DEFAULT_SLUG)}(?![a-z0-9-])")
_DEFAULT_ENV_PREFIX_RE = re.compile(
    rf"(?<![A-Z0-9_]){re.escape(DEFAULT_ENV_PREFIX)}(?=_|[^A-Z0-9_]|$)"
)
_ENV_EXAMPLE_RE = re.compile(r"^\.env(?:\.[A-Za-z0-9_-]+)?\.example$")

_DISPLAY_NAME_TOKEN = "display name"
_SLUG_TOKEN = "slug"
_ENV_PREFIX_TOKEN = "env prefix"
_PORT_TOKEN = "port"
_REQUIRED_TEMPLATE_TOKENS = frozenset(
    {_DISPLAY_NAME_TOKEN, _SLUG_TOKEN, _ENV_PREFIX_TOKEN, _PORT_TOKEN}
)


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
    if identity.name.splitlines() != [identity.name]:
        _fail("name must be a single line")
    if len(name) > MAX_NAME_LENGTH:
        _fail(f"name must be at most {MAX_NAME_LENGTH} characters")
    if not _DISPLAY_NAME_RE.fullmatch(name) or name in {"---", "..."}:
        _fail(
            "name may contain only Unicode letters, digits, underscores, spaces, "
            "dots, parentheses, and hyphens"
        )
    if name.startswith("- "):
        _fail("name must be safe as a plain configuration value")
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
                    *(
                        path
                        for path in docker.glob("*.example")
                        if _ENV_EXAMPLE_RE.fullmatch(path.name)
                    ),
                ]
            )

    candidates.append(root / ".gitlab-ci.yml")
    superpowers_docs = root / "docs/superpowers"
    candidates.extend(
        path
        for path in _matching_files(root, "docs", {".md"})
        if not path.is_relative_to(superpowers_docs)
    )

    safe_candidates: dict[str, Path] = {}
    for path in candidates:
        if _require_safe_file(root, path):
            safe_candidates[_relative_display(root, path)] = path
    return [safe_candidates[key] for key in sorted(safe_candidates)]


def _replacement_pattern(relative_path: str) -> re.Pattern[str]:
    alternatives = [
        (
            rf"(?P<port_assignment>(?<![A-Z0-9_]){DEFAULT_ENV_PREFIX}"
            rf"(?P<assignment_middle>_DEFAULT_PORT[ \t]*=[ \t]*)"
            rf"{DEFAULT_PORT}(?![0-9]))"
        ),
        (
            rf"(?P<port_yaml_key>^(?P<yaml_key_indent>[ \t]*){DEFAULT_ENV_PREFIX}"
            rf"(?P<yaml_key_middle>_DEFAULT_PORT[ \t]*:[ \t]*)"
            rf"(?P<yaml_key_open_quote>[\"']?){DEFAULT_PORT}"
            r"(?P<yaml_key_close_quote>[\"']?)(?![0-9]))"
        ),
    ]
    if relative_path.startswith("docker/docker-compose-") and relative_path.endswith(
        ".yml"
    ):
        alternatives.append(
            rf"(?P<compose_port>\$\{{{DEFAULT_ENV_PREFIX}_PORT:-{DEFAULT_PORT}\}})"
        )
    if relative_path.endswith((".yaml", ".yml")):
        alternatives.append(
            (
                rf"(?P<yaml_port>^(?P<yaml_port_prefix>[ \t]*port[ \t]*:[ \t]*)"
                rf"(?P<yaml_port_open_quote>[\"']?){DEFAULT_PORT}"
                r"(?P<yaml_port_close_quote>[\"']?)"
                r"(?P<yaml_port_suffix>[ \t]*(?:#.*)?\r?$))"
            )
        )
    alternatives.extend(
        [
            rf"(?P<display_name>{_DEFAULT_NAME_RE.pattern})",
            rf"(?P<slug>{_DEFAULT_SLUG_RE.pattern})",
            rf"(?P<env_prefix>{_DEFAULT_ENV_PREFIX_RE.pattern})",
        ]
    )
    return re.compile("|".join(alternatives), flags=re.MULTILINE)


def _transform_text(
    text: str, relative_path: str, identity: ProjectIdentity
) -> tuple[str, frozenset[str]]:
    hits: set[str] = set()
    port = str(identity.port)

    def replace(match: re.Match[str]) -> str:
        if match.group("port_assignment") is not None:
            hits.update({_ENV_PREFIX_TOKEN, _PORT_TOKEN})
            return f"{identity.env_prefix}{match.group('assignment_middle')}{port}"
        if match.group("port_yaml_key") is not None:
            hits.update({_ENV_PREFIX_TOKEN, _PORT_TOKEN})
            return (
                f"{match.group('yaml_key_indent')}{identity.env_prefix}"
                f"{match.group('yaml_key_middle')}{match.group('yaml_key_open_quote')}"
                f"{port}{match.group('yaml_key_close_quote')}"
            )
        if "compose_port" in match.re.groupindex and match.group("compose_port"):
            hits.update({_ENV_PREFIX_TOKEN, _PORT_TOKEN})
            return f"${{{identity.env_prefix}_PORT:-{port}}}"
        if "yaml_port" in match.re.groupindex and match.group("yaml_port"):
            hits.add(_PORT_TOKEN)
            return (
                f"{match.group('yaml_port_prefix')}{match.group('yaml_port_open_quote')}"
                f"{port}{match.group('yaml_port_close_quote')}"
                f"{match.group('yaml_port_suffix')}"
            )
        if match.group("display_name") is not None:
            hits.add(_DISPLAY_NAME_TOKEN)
            return identity.name
        if match.group("slug") is not None:
            hits.add(_SLUG_TOKEN)
            return identity.slug
        hits.add(_ENV_PREFIX_TOKEN)
        return identity.env_prefix

    transformed = _replacement_pattern(relative_path).sub(replace, text)
    return transformed, frozenset(hits)


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
    template_tokens: set[str] = set()

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
        transformed_text, hits = _transform_text(
            text, _relative_display(root, path), identity
        )
        template_tokens.update(hits)
        transformed[path] = transformed_text.encode("utf-8")

    missing_tokens = _REQUIRED_TEMPLATE_TOKENS - template_tokens
    if missing_tokens:
        _fail(
            "template is missing required default token(s): "
            + ", ".join(sorted(missing_tokens))
        )

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
        generated.append((real_env, example_content, 0o600))

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
            mode=0o600,
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


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _commit_writes(writes: list[_PlannedWrite]) -> None:
    staged: dict[Path, Path] = {}
    committed: list[_PlannedWrite] = []
    try:
        for write in writes:
            staged[write.target] = _stage_write(write)
        for write in writes:
            temp_path = staged[write.target]
            if write.original_content is None:
                try:
                    os.link(temp_path, write.target)
                except BaseException:
                    if _same_file(temp_path, write.target):
                        committed.append(write)
                    raise
                committed.append(write)
                temp_path.unlink()
                continue

            try:
                os.replace(temp_path, write.target)
            except BaseException:
                if not temp_path.exists():
                    committed.append(write)
                raise
            committed.append(write)
    except BaseException as error:
        rollback_errors: list[OSError] = []
        for write in reversed(committed):
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


def _acquire_lock(root: Path) -> Path:
    lock = root / LOCK_NAME
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise InitializationError(
            f"project initialization lock exists: {LOCK_NAME}"
        ) from error

    try:
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        lock.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    return lock


def initialize_project(root: Path, identity: ProjectIdentity) -> None:
    """Initialize a template copy in place with atomic rollback on failure."""
    root = root.resolve(strict=True)
    lock = _acquire_lock(root)
    try:
        marker = root / MARKER_NAME
        if _path_exists(marker):
            _fail(f"project is already initialized: {MARKER_NAME}")

        validated = _validate_identity(identity)
        writes = _plan_writes(root, validated)
        _commit_writes(writes)
    finally:
        lock.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize this NexusKernel Starter repository once.",
        allow_abbrev=False,
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
