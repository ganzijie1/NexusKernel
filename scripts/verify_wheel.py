#!/usr/bin/env python3
"""Verify wheel structure and metadata against the target project contract."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import parse_tag
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    canonicalize_version,
    parse_wheel_filename,
)


ROOT = Path(__file__).resolve().parents[1]
LEGACY_PACKAGE = "nexus" + "kernel"
PACKAGE_ROOTS = frozenset({"nexus_kernel", "app"})
DIST_INFO_FILES = frozenset({"METADATA", "WHEEL", "RECORD"})
SENSITIVE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
SENSITIVE_NAMES = frozenset({"credentials.json", "service-account.json"})


@dataclass(frozen=True, slots=True)
class WheelContract:
    distribution: str
    version: str
    requires_python: str
    dependencies: tuple[str, ...]
    optional_dependencies: dict[str, tuple[str, ...]]

    @property
    def dist_info_directory(self) -> str:
        distribution = canonicalize_name(self.distribution).replace("-", "_")
        version = canonicalize_version(self.version, strip_trailing_zero=False)
        return f"{distribution}-{version}.dist-info"


def load_project_contract(project_root: Path) -> WheelContract:
    pyproject = project_root / "pyproject.toml"
    try:
        with pyproject.open("rb") as stream:
            project = tomllib.load(stream)["project"]
        distribution = project["name"]
        version = project["version"]
        requires_python = project["requires-python"]
        raw_dependencies = project.get("dependencies", [])
        raw_optional_dependencies = project.get("optional-dependencies", {})
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(
            f"cannot load project wheel contract: {type(exc).__name__}"
        ) from exc

    if not all(
        isinstance(value, str) and value
        for value in (distribution, version, requires_python)
    ):
        raise ValueError("project wheel contract fields must be non-empty strings")
    if not isinstance(raw_dependencies, list) or not isinstance(
        raw_optional_dependencies, dict
    ):
        raise ValueError("project optional dependencies must be a table")
    dependencies = tuple(raw_dependencies)
    optional_dependencies: dict[str, tuple[str, ...]] = {}
    try:
        SpecifierSet(requires_python)
        canonicalize_version(version)
        if not all(
            isinstance(dependency, str) and dependency for dependency in dependencies
        ):
            raise ValueError
        for dependency in dependencies:
            Requirement(dependency)
        for extra, extra_dependencies in raw_optional_dependencies.items():
            if not isinstance(extra, str) or not isinstance(extra_dependencies, list):
                raise ValueError
            normalized_dependencies = tuple(extra_dependencies)
            if not all(
                isinstance(dependency, str) and dependency
                for dependency in normalized_dependencies
            ):
                raise ValueError
            for dependency in normalized_dependencies:
                Requirement(dependency)
            optional_dependencies[extra] = normalized_dependencies
    except (InvalidRequirement, InvalidSpecifier, ValueError) as exc:
        raise ValueError("project wheel contract contains invalid metadata") from exc
    return WheelContract(
        distribution, version, requires_python, dependencies, optional_dependencies
    )


def _unsafe_entry(entry: str) -> bool:
    path = PurePosixPath(entry)
    if not entry or "\\" in entry or path.is_absolute() or ".." in path.parts:
        return True
    if LEGACY_PACKAGE in path.parts or "__pycache__" in path.parts:
        return True
    name = path.name.casefold()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in SENSITIVE_NAMES
        or path.suffix.casefold() in SENSITIVE_SUFFIXES
    )


def _parse_metadata(content: bytes, label: str, errors: list[str]) -> Message | None:
    try:
        return BytesParser(policy=default).parsebytes(content)
    except (TypeError, ValueError) as exc:
        errors.append(f"invalid {label}: {type(exc).__name__}")
        return None


def _validate_filename(
    wheel: Path, contract: WheelContract, errors: list[str]
) -> set[str]:
    try:
        distribution, version, _build, tags = parse_wheel_filename(wheel.name)
    except InvalidWheelFilename:
        errors.append("invalid wheel filename")
        return set()

    if canonicalize_name(distribution) != canonicalize_name(contract.distribution):
        errors.append("wheel filename distribution does not match project")
    if canonicalize_version(str(version)) != canonicalize_version(contract.version):
        errors.append("wheel filename version does not match project")
    return {str(tag) for tag in tags}


def _validate_metadata(
    metadata: Message, contract: WheelContract, errors: list[str]
) -> None:
    if canonicalize_name(metadata.get("Name", "")) != canonicalize_name(
        contract.distribution
    ):
        errors.append("METADATA Name does not match project")
    if canonicalize_version(metadata.get("Version", "")) != canonicalize_version(
        contract.version
    ):
        errors.append("METADATA Version does not match project")
    requires_python = metadata.get("Requires-Python")
    try:
        matches = requires_python is not None and SpecifierSet(
            requires_python
        ) == SpecifierSet(contract.requires_python)
    except InvalidSpecifier:
        matches = False
    if not matches:
        errors.append(
            f"METADATA Requires-Python does not match project: {requires_python!r}"
        )

    expected_extras = Counter(
        canonicalize_name(extra) for extra in contract.optional_dependencies
    )
    actual_extras = Counter(
        canonicalize_name(extra) for extra in metadata.get_all("Provides-Extra", [])
    )
    if actual_extras != expected_extras:
        errors.append("METADATA Provides-Extra does not match project")

    expected_core_requirements = Counter(
        _normalize_requirement(dependency) for dependency in contract.dependencies
    )
    expected_optional_requirements = Counter(
        _normalize_requirement(_add_extra_marker(dependency, extra))
        for extra, dependencies in contract.optional_dependencies.items()
        for dependency in dependencies
    )
    actual_core_requirements: Counter[tuple[object, ...]] = Counter()
    actual_optional_requirements: Counter[tuple[object, ...]] = Counter()
    invalid_requirement = False
    for value in metadata.get_all("Requires-Dist", []):
        try:
            requirement = Requirement(value)
        except InvalidRequirement:
            invalid_requirement = True
            continue
        if requirement.marker is not None and "extra" in str(requirement.marker):
            actual_optional_requirements[
                _normalize_parsed_requirement(requirement)
            ] += 1
        else:
            actual_core_requirements[_normalize_parsed_requirement(requirement)] += 1
    if invalid_requirement or actual_core_requirements != expected_core_requirements:
        errors.append("METADATA core Requires-Dist does not match project")
    if (
        invalid_requirement
        or actual_optional_requirements != expected_optional_requirements
    ):
        errors.append("METADATA optional Requires-Dist does not match project")


def _add_extra_marker(dependency: str, extra: str) -> str:
    requirement, separator, marker = dependency.partition(";")
    if separator:
        return f'{requirement.strip()}; ({marker.strip()}) and extra == "{extra}"'
    return f'{requirement.strip()}; extra == "{extra}"'


def _normalize_requirement(requirement: str) -> tuple[object, ...]:
    return _normalize_parsed_requirement(Requirement(requirement))


def _normalize_parsed_requirement(requirement: Requirement) -> tuple[object, ...]:
    return (
        canonicalize_name(requirement.name),
        tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
        str(requirement.specifier),
        requirement.url,
        str(requirement.marker) if requirement.marker is not None else None,
    )


def _validate_wheel_metadata(
    wheel_metadata: Message, filename_tags: set[str], errors: list[str]
) -> None:
    if wheel_metadata.get("Wheel-Version") != "1.0":
        errors.append("WHEEL must declare Wheel-Version 1.0")
    if wheel_metadata.get("Root-Is-Purelib", "").casefold() != "true":
        errors.append("WHEEL must declare Root-Is-Purelib true")
    try:
        wheel_tags = {
            str(tag)
            for value in wheel_metadata.get_all("Tag", [])
            for tag in parse_tag(value)
        }
    except ValueError:
        wheel_tags = set()
    if not wheel_tags or wheel_tags != filename_tags:
        errors.append("WHEEL tags do not match wheel filename")


def _validate_record(
    content: bytes,
    archive: ZipFile,
    names: list[str],
    record_name: str,
    errors: list[str],
) -> None:
    try:
        rows = list(csv.reader(content.decode("utf-8").splitlines(), strict=True))
    except (UnicodeDecodeError, csv.Error):
        errors.append("invalid RECORD")
        return
    if any(len(row) != 3 or not row[0] for row in rows):
        errors.append("RECORD rows must contain exactly three columns")
        return

    paths = [row[0] for row in rows]
    if len(paths) != len(set(paths)):
        errors.append("RECORD contains duplicate paths")
    missing = sorted(set(names) - set(paths))
    unknown = sorted(set(paths) - set(names))
    if missing:
        errors.append(f"RECORD is missing archive entries: {', '.join(missing)}")
    if unknown:
        errors.append(f"RECORD contains unknown entries: {', '.join(unknown)}")

    for path, digest_field, size_field in rows:
        if path == record_name:
            if digest_field or size_field:
                errors.append("RECORD itself must have empty hash and size")
            continue
        if path not in names:
            continue
        data = archive.read(path)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        expected_digest = f"sha256={digest.decode('ascii')}"
        if digest_field != expected_digest:
            errors.append(f"RECORD hash does not match archive entry: {path}")
        if size_field != str(len(data)):
            errors.append(f"RECORD size does not match archive entry: {path}")


def validate_wheel(wheel: Path, contract: WheelContract) -> list[str]:
    errors: list[str] = []
    filename_tags = _validate_filename(wheel, contract, errors)
    try:
        with ZipFile(wheel) as archive:
            names = archive.namelist()
            for name, count in Counter(names).items():
                if count > 1:
                    errors.append(f"duplicate wheel entry: {name}")
            try:
                corrupt = archive.testzip()
            except BadZipFile:
                corrupt = "unknown"
            if corrupt is not None:
                errors.append(f"wheel CRC check failed: {corrupt}")
                return errors

            for name in names:
                if _unsafe_entry(name):
                    errors.append(f"unsafe wheel entry: {name}")

            dist_info_roots = {
                PurePosixPath(name).parts[0]
                for name in names
                if PurePosixPath(name).parts
                and PurePosixPath(name).parts[0].endswith(".dist-info")
            }
            if dist_info_roots != {contract.dist_info_directory}:
                errors.append(
                    "wheel must contain the unique project dist-info directory"
                )
            allowed_roots = PACKAGE_ROOTS | {contract.dist_info_directory}
            for name in names:
                path = PurePosixPath(name)
                if path.parts and path.parts[0] not in allowed_roots:
                    errors.append(f"unknown top-level entry: {name}")

            for package in sorted(PACKAGE_ROOTS):
                if not any(name.startswith(f"{package}/") for name in names):
                    errors.append(f"wheel does not contain {package} package")

            required = {
                label: f"{contract.dist_info_directory}/{label}"
                for label in DIST_INFO_FILES
            }
            for label, name in required.items():
                if names.count(name) != 1:
                    errors.append(f"wheel must contain exactly one {label} file")
            if any(names.count(name) != 1 for name in required.values()):
                return errors

            metadata = _parse_metadata(
                archive.read(required["METADATA"]), "METADATA", errors
            )
            wheel_metadata = _parse_metadata(
                archive.read(required["WHEEL"]), "WHEEL", errors
            )
            if metadata is not None:
                _validate_metadata(metadata, contract, errors)
            if wheel_metadata is not None:
                _validate_wheel_metadata(wheel_metadata, filename_tags, errors)
            _validate_record(
                archive.read(required["RECORD"]),
                archive,
                names,
                required["RECORD"],
                errors,
            )
    except (BadZipFile, OSError, RuntimeError) as exc:
        return [*errors, f"cannot read wheel: {type(exc).__name__}"]
    return errors


def _discover_wheel(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.glob("*.whl"))
    return [target] if target.suffix == ".whl" and target.is_file() else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    arguments = parser.parse_args()
    wheels = _discover_wheel(arguments.target)
    if len(wheels) != 1:
        print("expected exactly one wheel", file=sys.stderr)
        return 1
    try:
        contract = load_project_contract(arguments.project_root)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    errors = validate_wheel(wheels[0], contract)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Wheel contract passed: {wheels[0].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
