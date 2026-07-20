#!/usr/bin/env python3
"""Fail on tracked-style secrets, private addresses, or unsafe Docker context."""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pathspec
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

if TYPE_CHECKING:
    from scripts.delivery_contract import GENERATED_ENV_FILES, is_sensitive_artifact
elif __package__:
    from .delivery_contract import GENERATED_ENV_FILES, is_sensitive_artifact
else:
    from delivery_contract import GENERATED_ENV_FILES, is_sensitive_artifact


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".venv-win",
    ".venv-linux",
    ".venv-macos",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
}
TEXT_SUFFIXES = {
    ".py",
    ".yaml",
    ".yml",
    ".md",
    ".toml",
    ".example",
    ".env",
}
PRIVATE_IP = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
CREDENTIAL_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.I)
KEY_SHAPE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")
TEST_FIXTURE_MARKER = "security-test-" + "fixture"
_ENV_PLACEHOLDER = re.compile(
    r"^(?:\$\{[A-Za-z_][A-Za-z0-9_]*(?::-)?\}|"
    r"\$[A-Za-z_][A-Za-z0-9_]*|\$env:[A-Za-z_][A-Za-z0-9_]*|"
    r"%[A-Za-z_][A-Za-z0-9_]*%)$",
    re.I,
)
_TEXT_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:(?:export|readonly|declare|typeset)"
    r"(?:\s+(?:[-+][A-Za-z]+|--))*\s+)|(?:ENV|ARG|set)\s+)?(?:\$env:)?"
    r"[\"']?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)[\"']?\s*"
    r"(?:\+=|=|:)\s*(?P<value>.*)$",
    re.I,
)
_POWERSHELL_SET_ITEM = re.compile(
    r"^\s*Set-Item\s+(?:-Path\s+)?[\"']?Env:(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"[\"']?\s+(?:-Value\s+)?(?P<value>.*)$",
    re.I,
)
_POWERSHELL_SET_ITEM_VALUE_FIRST = re.compile(
    r"^\s*Set-Item\s+-Value\s+(?P<value>.+?)\s+-Path\s+"
    r"[\"']?Env:(?P<key>[A-Za-z_][A-Za-z0-9_]*)[\"']?\s*$",
    re.I,
)
_POWERSHELL_SET_ENVIRONMENT_VARIABLE = re.compile(
    r"^\s*\[Environment\]::SetEnvironmentVariable\(\s*[\"']"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)[\"']\s*,\s*(?P<value>[^,)]*)",
    re.I,
)
_MISSING = object()
_STATIC_UNSAFE = object()
_MAX_STATIC_TEXT = 65_536
_MAX_STATIC_DEPTH = 32

TEXT_SUFFIXES |= {
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".json",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".rst",
    ".txt",
    ".envrc",
}


def _is_real_environment_file(path: Path) -> bool:
    return path.name == ".env" or (
        path.name.startswith(".env.") and not path.name.endswith(".example")
    )


def _is_environment_directory(path: Path) -> bool:
    return path.name.casefold().startswith(".env")


def _is_reparse_point(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _files() -> list[Path]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(
        ROOT, topdown=True, followlinks=False
    ):
        base = Path(directory)
        retained_directories: list[str] = []
        for name in sorted(dirnames):
            path = base / name
            try:
                metadata = path.lstat()
            except OSError:
                files.append(path)
                continue
            if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(metadata):
                files.append(path)
                continue
            if _is_environment_directory(path) or is_sensitive_artifact(path):
                files.append(path)
                continue
            if name in EXCLUDED_PARTS or name.endswith(".egg-info"):
                continue
            retained_directories.append(name)
        dirnames[:] = retained_directories
        for name in sorted(filenames):
            path = base / name
            try:
                metadata = path.lstat()
            except OSError:
                files.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(metadata):
                files.append(path)
                continue
            if (
                path.suffix.casefold() in TEXT_SUFFIXES
                or _is_real_environment_file(path)
                or path.name == ".envrc"
                or is_sensitive_artifact(path)
                or path.name in {"Dockerfile", ".gitlab-ci.yml"}
            ):
                files.append(path)
    return files


def _tracked_files() -> frozenset[str]:
    if not (ROOT / ".git").exists():
        return frozenset()
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--cached", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return GENERATED_ENV_FILES
    if result.returncode != 0:
        return GENERATED_ENV_FILES
    return frozenset(
        path.decode("utf-8", errors="surrogateescape")
        for path in result.stdout.split(b"\0")
        if path
    )


def _ignore_spec(name: str) -> tuple[pathspec.GitIgnoreSpec | None, str | None]:
    path = ROOT / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, f"unscannable text file: {name}:1"
    if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(metadata):
        return None, f"unscannable file type: {name}:1"
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
        return pathspec.GitIgnoreSpec.from_lines(lines), None
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None, f"unscannable text file: {name}:1"


def _allowed_generated_env(
    path: Path,
    relative: str,
    tracked: frozenset[str],
    git_ignore: pathspec.GitIgnoreSpec | None,
    docker_ignore: pathspec.GitIgnoreSpec | None,
) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    # Windows st_mode does not encode the NTFS DACL; reparse/type checks remain valid.
    private_mode = os.name == "nt" or stat.S_IMODE(metadata.st_mode) == 0o600
    return (
        relative in GENERATED_ENV_FILES
        and stat.S_ISREG(metadata.st_mode)
        and not _is_reparse_point(metadata)
        and private_mode
        and relative not in tracked
        and git_ignore is not None
        and git_ignore.match_file(relative)
        and docker_ignore is not None
        and docker_ignore.match_file(relative)
    )


def _is_sensitive_key(name: str) -> bool:
    with_camel_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    parts = tuple(
        part
        for part in re.split(r"[^a-z0-9]+", with_camel_boundaries.casefold())
        if part
    )
    if any(
        part
        in {
            "token",
            "password",
            "passwd",
            "secret",
            "credential",
            "credentials",
            "authorization",
        }
        for part in parts
    ):
        return True
    collapsed = "".join(parts)
    return collapsed in {"apikey", "accesstoken", "refreshtoken"}


def _unquote(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] in {"'", '"'} and stripped[-1] == stripped[0]:
        return stripped[1:-1].strip()
    return stripped


def _is_safe_text(value: str) -> bool:
    normalized = _unquote(value)
    return (
        not normalized
        or normalized.casefold() in {"none", "null", "~"}
        or _ENV_PLACEHOLDER.fullmatch(normalized) is not None
    )


def _is_safe_literal(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return _is_safe_text(value)
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return not value
    return False


def _bounded_text(value: str) -> str | object:
    return value if len(value) <= _MAX_STATIC_TEXT else _STATIC_UNSAFE


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _python_binop(node: ast.BinOp, depth: int) -> object:
    left = _python_literal(node.left, depth=depth + 1)
    right = _python_literal(node.right, depth=depth + 1)
    if isinstance(node.op, ast.Add):
        if isinstance(left, str) and isinstance(right, str):
            return _bounded_text(left + right)
        if left is _STATIC_UNSAFE or right is _STATIC_UNSAFE:
            return _STATIC_UNSAFE
        return _MISSING
    if not isinstance(node.op, ast.Mult):
        return _MISSING
    text: str | None = None
    count: int | None = None
    if isinstance(left, str) and isinstance(right, int):
        text, count = left, right
    elif isinstance(right, str) and isinstance(left, int):
        text, count = right, left
    if text is None or count is None or count < 0:
        return _MISSING
    if len(text) * count > _MAX_STATIC_TEXT:
        return _STATIC_UNSAFE
    return text * count


def _python_joined_string(node: ast.JoinedStr, depth: int) -> object:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
            continue
        if not isinstance(value, ast.FormattedValue):
            return _MISSING
        if value.conversion != -1 or value.format_spec is not None:
            return _STATIC_UNSAFE
        if value.format_spec is None:
            literal = _python_literal(value.value, depth=depth + 1)
            if isinstance(literal, (str, int, float, bool)):
                parts.append(str(literal))
                continue
        return _MISSING
    return _bounded_text("".join(parts))


def _python_call_literal(node: ast.Call, depth: int) -> object:
    name = _call_name(node.func)
    if (
        name in {"SecretStr", "SecretBytes", "str", "bytes"}
        and len(node.args) == 1
        and not node.keywords
    ):
        return _python_literal(node.args[0], depth=depth + 1)
    if not (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        return _MISSING
    separator = _python_literal(node.func.value, depth=depth + 1)
    values = [_python_literal(value, depth=depth + 1) for value in node.args[0].elts]
    if isinstance(separator, str) and all(isinstance(value, str) for value in values):
        return _bounded_text(separator.join(cast("list[str]", values)))
    return _MISSING


def _python_literal(node: ast.AST, *, depth: int = 0) -> object:
    if depth > _MAX_STATIC_DEPTH:
        return _MISSING
    try:
        return ast.literal_eval(node)
    except (RecursionError, TypeError, ValueError):
        if isinstance(node, ast.BinOp):
            return _python_binop(node, depth)
        if isinstance(node, ast.JoinedStr):
            return _python_joined_string(node, depth)
        if isinstance(node, ast.Call):
            return _python_call_literal(node, depth)
        return _MISSING


def _target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        value = _python_literal(node.slice)
        return value if isinstance(value, str) else None
    if isinstance(node, ast.Starred):
        return _target_name(node.value)
    return None


class _SensitiveAssignmentVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.lines: set[int] = set()

    def _check(self, name: str | None, value: ast.AST, line: int) -> None:
        literal = _python_literal(value)
        if (
            name is not None
            and _is_sensitive_key(name)
            and literal is not _MISSING
            and not _is_safe_literal(literal)
        ):
            self.lines.add(line)

    def _check_target(self, target: ast.AST, value: ast.AST, line: int) -> None:
        if isinstance(target, (ast.List, ast.Tuple)):
            if isinstance(value, (ast.List, ast.Tuple)) and len(target.elts) == len(
                value.elts
            ):
                for child_target, child_value in zip(
                    target.elts, value.elts, strict=True
                ):
                    self._check_target(child_target, child_value, line)
                return
            for child_target in target.elts:
                self._check_target(child_target, value, line)
            return
        self._check(_target_name(target), value, line)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_target(target, node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._check(_target_name(node.target), node.value, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.op, ast.Add):
            self._check(_target_name(node.target), node.value, node.lineno)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._check(_target_name(node.target), node.value, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            self._check(keyword.arg, keyword.value, keyword.value.lineno)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                continue
            literal_key = _python_literal(key)
            self._check(
                literal_key if isinstance(literal_key, str) else None,
                value,
                key.lineno,
            )
        self.generic_visit(node)


def _python_assignment_lines(content: str) -> set[int] | None:
    try:
        tree = ast.parse(content)
        visitor = _SensitiveAssignmentVisitor()
        visitor.visit(tree)
    except (RecursionError, SyntaxError, ValueError):
        return None
    return visitor.lines


def _yaml_value_is_safe(node: Node) -> bool:
    if isinstance(node, ScalarNode):
        return node.tag.endswith(":null") or _is_safe_text(node.value)
    if isinstance(node, (MappingNode, SequenceNode)):
        return not node.value
    return False


def _yaml_assignment_lines(content: str) -> set[int] | None:
    lines: set[int] = set()
    visited: set[int] = set()

    def visit(node: Node) -> None:
        identity = id(node)
        if identity in visited:
            return
        visited.add(identity)
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if (
                    isinstance(key, ScalarNode)
                    and _is_sensitive_key(key.value)
                    and not _yaml_value_is_safe(value)
                ):
                    lines.add(key.start_mark.line + 1)
                visit(value)
        elif isinstance(node, SequenceNode):
            for value in node.value:
                visit(value)

    try:
        for document in yaml.compose_all(content, Loader=yaml.SafeLoader):
            if document is not None:
                visited.clear()
                visit(document)
    except (RecursionError, yaml.YAMLError):
        return None
    return lines


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-standard JSON constant")


def _json_assignment_lines(content: str) -> set[int] | None:
    try:
        json.loads(
            content,
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, TypeError, ValueError):
        return None
    return _yaml_assignment_lines(content)


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote is not None:
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
            continue
        if character == "#" and quote is None:
            return value[:index]
    return value


def _text_assignment_lines(content: str) -> set[int]:
    findings: set[int] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        match = (
            _TEXT_ASSIGNMENT.fullmatch(line)
            or _POWERSHELL_SET_ITEM.fullmatch(line)
            or _POWERSHELL_SET_ITEM_VALUE_FIRST.fullmatch(line)
            or _POWERSHELL_SET_ENVIRONMENT_VARIABLE.match(line)
        )
        if match is None or not _is_sensitive_key(match.group("key")):
            continue
        value = _strip_inline_comment(match.group("value")).strip().removesuffix(",")
        if not _is_safe_text(value):
            findings.add(line_number)
    return findings


def _assignment_lines(path: Path, content: str) -> set[int] | None:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        return _python_assignment_lines(content)
    if suffix == ".json":
        return _json_assignment_lines(content)
    if suffix in {".yaml", ".yml"} or path.name == ".gitlab-ci.yml":
        return _yaml_assignment_lines(content)
    return _text_assignment_lines(content)


def _ignore_contract() -> tuple[
    pathspec.GitIgnoreSpec | None,
    pathspec.GitIgnoreSpec | None,
    set[str],
    list[str],
]:
    errors: list[str] = []
    git_ignore, git_ignore_error = _ignore_spec(".gitignore")
    docker_ignore, docker_ignore_error = _ignore_spec(".dockerignore")
    invalid_ignore_files: set[str] = set()
    for name, error in (
        (".gitignore", git_ignore_error),
        (".dockerignore", docker_ignore_error),
    ):
        if error is not None:
            errors.append(error)
            invalid_ignore_files.add(name)
    return git_ignore, docker_ignore, invalid_ignore_files, errors


def _text_findings(
    relative: str, content: str, assignment_lines: set[int]
) -> list[str]:
    errors: list[str] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        has_security_finding = (
            any(
                pattern.search(line)
                for pattern in (KEY_SHAPE, CREDENTIAL_URL, PRIVATE_IP)
            )
            or line_number in assignment_lines
        )
        if TEST_FIXTURE_MARKER in line and relative.startswith("tests/"):
            continue
        if TEST_FIXTURE_MARKER in line and has_security_finding:
            errors.append(
                f"unapproved {TEST_FIXTURE_MARKER} suppression: "
                f"{relative}:{line_number}"
            )
        if KEY_SHAPE.search(line):
            errors.append(f"secret-shaped key: {relative}:{line_number}")
        if CREDENTIAL_URL.search(line):
            errors.append(f"credential-bearing URL: {relative}:{line_number}")
        if PRIVATE_IP.search(line):
            errors.append(f"private address: {relative}:{line_number}")
        if line_number in assignment_lines:
            errors.append(f"sensitive literal assignment: {relative}:{line_number}")
    return errors


def _scan_path(
    path: Path,
    tracked: frozenset[str],
    git_ignore: pathspec.GitIgnoreSpec | None,
    docker_ignore: pathspec.GitIgnoreSpec | None,
    invalid_ignore_files: set[str],
) -> list[str]:
    relative = path.relative_to(ROOT).as_posix()
    if relative in invalid_ignore_files:
        return []
    try:
        metadata = path.lstat()
    except OSError:
        return [f"unscannable text file: {relative}:1"]
    if is_sensitive_artifact(path):
        return [f"sensitive artifact: {relative}:1"]
    if stat.S_ISDIR(metadata.st_mode) and _is_environment_directory(path):
        return [f"real environment file is present: {relative}:1"]
    if _is_real_environment_file(path):
        if _allowed_generated_env(path, relative, tracked, git_ignore, docker_ignore):
            return []
        return [f"real environment file is present: {relative}:1"]
    if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(metadata):
        return [f"unscannable file type: {relative}:1"]
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return [f"unscannable text file: {relative}:1"]
    assignment_lines = _assignment_lines(path, content)
    if assignment_lines is None:
        return [f"unscannable structured text: {relative}:1"]
    return _text_findings(relative, content, assignment_lines)


def scan() -> list[str]:
    git_ignore, docker_ignore, invalid_ignore_files, errors = _ignore_contract()
    tracked = _tracked_files()
    for path in _files():
        errors.extend(
            _scan_path(
                path,
                tracked,
                git_ignore,
                docker_ignore,
                invalid_ignore_files,
            )
        )

    for sample in (".env", "docker/.env.prod", "nested/service/.env.secret"):
        if docker_ignore is None or not docker_ignore.match_file(sample):
            errors.append(f"Docker context does not exclude: {sample}:1")
    return errors


def main() -> int:
    errors = scan()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Security contract scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
