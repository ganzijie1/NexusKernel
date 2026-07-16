#!/usr/bin/env python3
"""Fail on tracked-style secrets, private addresses, or unsafe Docker context."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pathspec
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


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
GENERATED_ENV_FILES = frozenset(
    {"docker/.env.dev", "docker/.env.preprod", "docker/.env.prod"}
)
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".toml", ".example"}
PRIVATE_IP = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
CREDENTIAL_URL = re.compile(  # security-pattern
    r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.I
)
KEY_SHAPE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")  # security-pattern
PATTERN_MARKER = "security-" + "pattern"
TEST_FIXTURE_MARKER = "security-test-" + "fixture"
PATTERN_MARKER_ALLOWED = {
    "scripts/security_scan.py",
    "nexus_kernel/observability/logging.py",
}
_ENV_PLACEHOLDER = re.compile(
    r"^(?:\$\{[A-Za-z_][A-Za-z0-9_]*(?::-)?\}|"
    r"\$[A-Za-z_][A-Za-z0-9_]*|\$env:[A-Za-z_][A-Za-z0-9_]*|"
    r"%[A-Za-z_][A-Za-z0-9_]*%)$",
    re.I,
)
_TEXT_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:export|ENV|ARG|set)\s+)?(?:\$env:)?"
    r"[\"']?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)[\"']?\s*"
    r"(?:=|:)\s*(?P<value>.*)$",
    re.I,
)
_MISSING = object()

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
}


def _is_real_environment_file(path: Path) -> bool:
    return path.name == ".env" or (
        path.name.startswith(".env.") and not path.name.endswith(".example")
    )


def _files() -> list[Path]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(
        ROOT, topdown=True, followlinks=False
    ):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in EXCLUDED_PARTS and not name.endswith(".egg-info")
        )
        base = Path(directory)
        for name in sorted(filenames):
            path = base / name
            if (
                path.suffix.casefold() in TEXT_SUFFIXES
                or _is_real_environment_file(path)
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


def _ignore_spec(name: str) -> pathspec.GitIgnoreSpec | None:
    try:
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return pathspec.GitIgnoreSpec.from_lines(lines)


def _allowed_generated_env(
    relative: str,
    tracked: frozenset[str],
    git_ignore: pathspec.GitIgnoreSpec | None,
    docker_ignore: pathspec.GitIgnoreSpec | None,
) -> bool:
    return (
        relative in GENERATED_ENV_FILES
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


def _python_literal(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = _python_literal(node.left)
            right = _python_literal(node.right)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
        return _MISSING


def _target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        value = _python_literal(node.slice)
        return value if isinstance(value, str) else None
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

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check(_target_name(target), node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
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


def _python_assignment_lines(content: str) -> set[int]:
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return set()
    visitor = _SensitiveAssignmentVisitor()
    visitor.visit(tree)
    return visitor.lines


def _yaml_value_is_safe(node: Node) -> bool:
    if isinstance(node, ScalarNode):
        return node.tag.endswith(":null") or _is_safe_text(node.value)
    if isinstance(node, (MappingNode, SequenceNode)):
        return not node.value
    return False


def _yaml_assignment_lines(content: str) -> set[int]:
    lines: set[int] = set()

    def visit(node: Node) -> None:
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
                visit(document)
    except yaml.YAMLError:
        return set()
    return lines


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
        match = _TEXT_ASSIGNMENT.fullmatch(line)
        if match is None or not _is_sensitive_key(match.group("key")):
            continue
        value = _strip_inline_comment(match.group("value")).strip().removesuffix(",")
        if not _is_safe_text(value):
            findings.add(line_number)
    return findings


def _assignment_lines(path: Path, content: str) -> set[int]:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        return _python_assignment_lines(content)
    if suffix in {".yaml", ".yml", ".json"} or path.name == ".gitlab-ci.yml":
        return _yaml_assignment_lines(content)
    return _text_assignment_lines(content)


def scan() -> list[str]:
    errors: list[str] = []
    tracked = _tracked_files()
    git_ignore = _ignore_spec(".gitignore")
    docker_ignore = _ignore_spec(".dockerignore")
    for path in _files():
        relative = path.relative_to(ROOT).as_posix()
        if _is_real_environment_file(path):
            if _allowed_generated_env(relative, tracked, git_ignore, docker_ignore):
                continue
            errors.append(f"real environment file is present: {relative}")
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        assignment_lines = _assignment_lines(path, content)
        for line_number, line in enumerate(content.splitlines(), start=1):
            has_security_finding = (
                any(
                    pattern.search(line)
                    for pattern in (KEY_SHAPE, CREDENTIAL_URL, PRIVATE_IP)
                )
                or line_number in assignment_lines
            )
            if PATTERN_MARKER in line:
                if relative not in PATTERN_MARKER_ALLOWED:
                    errors.append(
                        f"unapproved {PATTERN_MARKER} suppression: "
                        f"{relative}:{line_number}"
                    )
                continue
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

    for sample in (".env", "docker/.env.prod", "nested/service/.env.secret"):
        if docker_ignore is None or not docker_ignore.match_file(sample):
            errors.append(f"Docker context does not exclude: {sample}")
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
