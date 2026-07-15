#!/usr/bin/env python3
"""Fail on tracked-style secrets, private addresses, or unsafe Docker context."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pathspec


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".venv-win",
    ".venv-linux",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
}
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".toml", ".example"}
PRIVATE_IP = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
CREDENTIAL_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.I)
KEY_SHAPE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")


def _files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.parts)
        and (
            path.suffix in TEXT_SUFFIXES
            or path.name in {"Dockerfile", ".gitlab-ci.yml"}
        )
    )


def scan() -> list[str]:
    errors: list[str] = []
    for path in _files():
        relative = path.relative_to(ROOT).as_posix()
        if path.name == ".env" or (
            path.name.startswith(".env.") and not path.name.endswith(".example")
        ):
            errors.append(f"real environment file is present: {relative}")
            continue
        if relative == "scripts/security_scan.py" or relative.startswith("tests/"):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(content.splitlines(), start=1):
            if "security-pattern" in line:
                continue
            if KEY_SHAPE.search(line):
                errors.append(f"secret-shaped key: {relative}:{line_number}")
            if CREDENTIAL_URL.search(line):
                errors.append(f"credential-bearing URL: {relative}:{line_number}")
            if PRIVATE_IP.search(line):
                errors.append(f"private address: {relative}:{line_number}")

    ignore = pathspec.GitIgnoreSpec.from_lines(
        (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    for sample in (".env", "docker/.env.prod", "nested/service/.env.secret"):
        if not ignore.match_file(sample):
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
