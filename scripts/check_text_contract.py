#!/usr/bin/env python3
"""Validate the complete tracked text snapshot and declared worktree EOL policy."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git(
    root: Path, arguments: list[str], *, input_bytes: bytes | None = None
) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def _tracked_paths(root: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    inventory = _git(root, ["ls-files", "--stage", "-z"])
    if inventory is None:
        return [], ["git inventory unavailable: .:1"]
    paths: list[tuple[str, str, str]] = []
    for item in inventory.split(b"\0"):
        if not item:
            continue
        try:
            metadata, encoded_path = item.split(b"\t", maxsplit=1)
            mode, object_id, stage = metadata.split(b" ")
        except ValueError:
            return [], ["invalid git inventory response: .:1"]
        if stage != b"0":
            return [], ["unmerged git inventory: .:1"]
        paths.append(
            (
                encoded_path.decode("utf-8", errors="surrogateescape"),
                mode.decode("ascii", errors="strict"),
                object_id.decode("ascii", errors="strict"),
            )
        )
    return paths, []


def _attributes(
    root: Path, indexed_paths: list[tuple[str, str, str]]
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    if not indexed_paths:
        return {}, []
    paths = [path for path, _mode, _object_id in indexed_paths]
    payload = (
        b"\0".join(path.encode("utf-8", errors="surrogateescape") for path in paths)
        + b"\0"
    )
    output = _git(
        root,
        ["check-attr", "-z", "--cached", "--stdin", "text", "eol"],
        input_bytes=payload,
    )
    if output is None:
        return {}, ["git attributes unavailable: .gitattributes:1"]
    fields = output.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) != len(paths) * 6:
        return {}, ["invalid git attributes response: .gitattributes:1"]

    attributes: dict[str, tuple[str, str]] = {}
    for index in range(0, len(fields), 6):
        first_path, first_name, text_value, second_path, second_name, eol_value = (
            field.decode("utf-8", errors="surrogateescape")
            for field in fields[index : index + 6]
        )
        if first_path != second_path or first_name != "text" or second_name != "eol":
            return {}, ["invalid git attributes response: .gitattributes:1"]
        attributes[first_path] = (text_value, eol_value)
    return attributes, []


def _indexed_blobs(
    root: Path, indexed_paths: list[tuple[str, str, str]]
) -> tuple[dict[str, bytes], list[str]]:
    payload = b"".join(
        object_id.encode("ascii") + b"\n" for _path, _mode, object_id in indexed_paths
    )
    output = _git(root, ["cat-file", "--batch"], input_bytes=payload)
    if output is None:
        return {}, ["git object inventory unavailable: .:1"]

    blobs: dict[str, bytes] = {}
    position = 0
    for relative, _mode, object_id in indexed_paths:
        header_end = output.find(b"\n", position)
        if header_end < 0:
            return {}, ["invalid git object response: .:1"]
        header = output[position:header_end].split(b" ")
        if (
            len(header) != 3
            or header[0].decode("ascii") != object_id
            or header[1] != b"blob"
        ):
            return {}, ["invalid git object response: .:1"]
        try:
            size = int(header[2])
        except ValueError:
            return {}, ["invalid git object response: .:1"]
        position = header_end + 1
        blob_end = position + size
        if blob_end >= len(output) or output[blob_end : blob_end + 1] != b"\n":
            return {}, ["invalid git object response: .:1"]
        blobs[relative] = output[position:blob_end]
        position = blob_end + 1
    if position != len(output):
        return {}, ["invalid git object response: .:1"]
    return blobs, []


def _eol_errors(relative: str, data: bytes, expected: str) -> list[str]:
    line = 1
    for index, byte in enumerate(data):
        if byte == 0x0A:
            if expected == "lf" and index > 0 and data[index - 1] == 0x0D:
                return [f"unexpected CRLF: {relative}:{line}"]
            elif expected == "crlf" and (index == 0 or data[index - 1] != 0x0D):
                return [f"unexpected LF: {relative}:{line}"]
            line += 1
        elif byte == 0x0D and (index + 1 == len(data) or data[index + 1] != 0x0A):
            return [f"unexpected CR: {relative}:{line}"]
    return []


def _check_path(
    relative: str,
    mode: str,
    data: bytes,
    text_attribute: str,
    eol_attribute: str,
) -> list[str]:
    if mode not in {"100644", "100755"}:
        return [f"unsupported tracked file type: {relative}:1"]
    if text_attribute == "unset" or (text_attribute == "auto" and b"\0" in data):
        return []
    if text_attribute not in {"auto", "set"}:
        return [f"missing text attribute: {relative}:1"]
    if eol_attribute not in {"lf", "crlf"}:
        return [f"missing eol attribute: {relative}:1"]
    try:
        content = data.decode("utf-8", errors="strict")
    except UnicodeError:
        return [f"non-UTF-8 tracked text: {relative}:1"]

    # Git stores normalized text as LF; the eol attribute controls checkout only.
    errors = _eol_errors(relative, data, "lf")
    if data and not data.endswith(b"\n"):
        errors.append(f"missing final newline: {relative}:{content.count(chr(10)) + 1}")
    for line_number, line in enumerate(content.splitlines(), start=1):
        if line.endswith((" ", "\t")):
            errors.append(f"trailing whitespace: {relative}:{line_number}")
    return errors


def check(root: Path = ROOT) -> list[str]:
    """Return stable path-and-line findings for the active Git index snapshot."""

    root = root.resolve()
    indexed_paths, errors = _tracked_paths(root)
    attributes, attribute_errors = _attributes(root, indexed_paths)
    errors.extend(attribute_errors)
    if attribute_errors:
        return errors
    blobs, blob_errors = _indexed_blobs(root, indexed_paths)
    errors.extend(blob_errors)
    if blob_errors:
        return errors
    for relative, mode, _object_id in indexed_paths:
        text_attribute, eol_attribute = attributes[relative]
        errors.extend(
            _check_path(
                relative,
                mode,
                blobs[relative],
                text_attribute,
                eol_attribute,
            )
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    arguments = parser.parse_args(argv)
    errors = check(arguments.root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Tracked text contract passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
