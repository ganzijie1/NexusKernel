from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from scripts import verify_wheel


ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / "scripts/verify_wheel.py"


def _write_project(
    root: Path,
    *,
    name: str = "nexus-kernel-starter",
    version: str = "0.2.0",
    requires_python: str = ">=3.11,<3.14",
    dependencies: tuple[str, ...] = ("httpx==0.28.1",),
    optional_dependencies: dict[str, tuple[str, ...]] | None = None,
) -> object:
    optional_dependencies = optional_dependencies or {
        "ai": ("openai==2.40.0",),
        "postgres": ("asyncpg==0.31.0",),
    }
    optional_lines = ["[project.optional-dependencies]"]
    optional_lines.extend(
        f"{extra} = [{', '.join(repr(dependency) for dependency in dependencies)}]"
        for extra, dependencies in optional_dependencies.items()
    )
    (root / "pyproject.toml").write_text(
        "\n".join(
            (
                "[project]",
                f'name = "{name}"',
                f'version = "{version}"',
                f'requires-python = "{requires_python}"',
                f"dependencies = [{', '.join(repr(dependency) for dependency in dependencies)}]",
                "",
                *optional_lines,
                "",
            )
        ),
        encoding="utf-8",
    )
    return verify_wheel.load_project_contract(root)


def _metadata(
    contract: object,
    *,
    requires_python: str | None = None,
    provided_extras: tuple[str, ...] | None = None,
    extra_requirements: tuple[str, ...] | None = None,
    core_requirements: tuple[str, ...] | None = None,
) -> str:
    if core_requirements is None:
        core_requirements = contract.dependencies
    if provided_extras is None:
        provided_extras = tuple(contract.optional_dependencies)
    if extra_requirements is None:
        extra_requirements = tuple(
            f'{dependency}; extra == "{extra}"'
            for extra, dependencies in contract.optional_dependencies.items()
            for dependency in dependencies
        )
    return "\n".join(
        (
            "Metadata-Version: 2.4",
            f"Name: {contract.distribution}",
            f"Version: {contract.version}",
            f"Requires-Python: {requires_python or contract.requires_python}",
            *(f"Requires-Dist: {requirement}" for requirement in core_requirements),
            *(f"Provides-Extra: {extra}" for extra in provided_extras),
            *(f"Requires-Dist: {requirement}" for requirement in extra_requirements),
            "",
            "",
        )
    )


def _write_wheel(
    path: Path,
    contract: object,
    *,
    extra_entries: tuple[str, ...] = (),
    omitted_dist_info: frozenset[str] = frozenset(),
    metadata: str | None = None,
    duplicate_entry: str | None = None,
    compression: int = ZIP_DEFLATED,
) -> None:
    dist_info = contract.dist_info_directory
    entries = {
        "nexus_kernel/__init__.py": "",
        "app/__init__.py": "",
        f"{dist_info}/METADATA": metadata or _metadata(contract),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: tests\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n\n"
        ),
    }
    for entry in extra_entries:
        entries[entry] = "crc-payload-unique" if entry.endswith("payload.txt") else "x"
    record_name = f"{dist_info}/RECORD"
    record_rows = []
    for entry, content in entries.items():
        data = content.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        record_rows.append(f"{entry},sha256={digest.decode('ascii')},{len(data)}\n")
    entries[record_name] = "".join((*record_rows, f"{record_name},,\n"))

    with ZipFile(path, "w", compression) as archive:
        for entry, content in entries.items():
            if entry.rsplit("/", 1)[-1] not in omitted_dist_info:
                archive.writestr(entry, content)
        if duplicate_entry is not None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(duplicate_entry, "duplicate")


def _wheel_path(
    root: Path,
    contract: object,
    *,
    filename_name: str | None = None,
    filename_version: str | None = None,
) -> Path:
    distribution = (filename_name or contract.distribution).replace("-", "_")
    version = filename_version or contract.version
    return root / f"{distribution}-{version}-py3-none-any.whl"


def test_valid_wheel_uses_project_contract(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    assert contract.dist_info_directory == "nexus_kernel_starter-0.2.0.dist-info"
    assert contract.dependencies == ("httpx==0.28.1",)
    assert contract.optional_dependencies == {
        "ai": ("openai==2.40.0",),
        "postgres": ("asyncpg==0.31.0",),
    }
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract)

    assert verify_wheel.validate_wheel(wheel, contract) == []


def _rewrite_record(wheel: Path, transform: Callable[[str], str]) -> None:
    with ZipFile(wheel) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    record_name = next(name for name in entries if name.endswith(".dist-info/RECORD"))
    entries[record_name] = transform(entries[record_name].decode("utf-8")).encode(
        "utf-8"
    )
    with ZipFile(wheel, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def _replace_record_text(wheel: Path, old: str, new: str) -> None:
    _rewrite_record(wheel, lambda content: content.replace(old, new, 1))


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            "nexus_kernel/__init__.py,sha256=",
            "nexus_kernel/__init__.py,sha256=x",
            "hash",
        ),
        (",0\napp/__init__.py", ",99\napp/__init__.py", "size"),
    ],
)
def test_wheel_rejects_tampered_record_hash_or_size(
    tmp_path: Path, old: str, new: str, expected: str
) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract)
    _replace_record_text(wheel, old, new)

    assert (
        expected in "\n".join(verify_wheel.validate_wheel(wheel, contract)).casefold()
    )


def test_wheel_requires_empty_record_self_hash_and_size(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract)
    _replace_record_text(wheel, "/RECORD,,", "/RECORD,sha256=invalid,1")

    assert (
        "record itself"
        in "\n".join(verify_wheel.validate_wheel(wheel, contract)).casefold()
    )


def test_wheel_rejects_malformed_record_rows(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract)
    _replace_record_text(wheel, "/RECORD,,", "/RECORD,")

    assert (
        "three columns"
        in "\n".join(verify_wheel.validate_wheel(wheel, contract)).casefold()
    )


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        (lambda content: "\n".join(content.splitlines()[1:]) + "\n", "missing"),
        (
            lambda content: content.splitlines()[0] + "\n" + content,
            "duplicate paths",
        ),
        (lambda content: "unknown.py,sha256=invalid,0\n" + content, "unknown"),
    ],
    ids=("missing-entry", "duplicate-entry", "unknown-entry"),
)
def test_wheel_rejects_inexact_record_path_inventory(
    tmp_path: Path, transform: Callable[[str], str], expected: str
) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract)
    _rewrite_record(wheel, transform)

    assert (
        expected in "\n".join(verify_wheel.validate_wheel(wheel, contract)).casefold()
    )


@pytest.mark.parametrize(
    "core_requirements",
    [(), ("httpx==0.28.1", "unknown==1.0")],
)
def test_wheel_rejects_inexact_core_dependency_metadata(
    tmp_path: Path, core_requirements: tuple[str, ...]
) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(
        wheel,
        contract,
        metadata=_metadata(contract, core_requirements=core_requirements),
    )

    assert "core Requires-Dist" in "\n".join(
        verify_wheel.validate_wheel(wheel, contract)
    )


@pytest.mark.parametrize(
    ("provided_extras", "extra_requirements", "expected"),
    [
        (("ai",), None, "Provides-Extra"),
        (("ai", "postgres", "unknown"), None, "Provides-Extra"),
        (None, ('openai==2.40.0; extra == "ai"',), "Requires-Dist"),
        (
            None,
            (
                'openai==2.40.0; extra == "ai"',
                'asyncpg==0.31.0; extra == "postgres"',
                'unknown==1.0; extra == "postgres"',
            ),
            "Requires-Dist",
        ),
    ],
)
def test_wheel_rejects_inexact_optional_dependency_metadata(
    tmp_path: Path,
    provided_extras: tuple[str, ...] | None,
    extra_requirements: tuple[str, ...] | None,
    expected: str,
) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(
        wheel,
        contract,
        metadata=_metadata(
            contract,
            provided_extras=provided_extras,
            extra_requirements=extra_requirements,
        ),
    )

    assert expected in "\n".join(verify_wheel.validate_wheel(wheel, contract))


def test_initialized_distribution_name_is_not_hardcoded(tmp_path: Path) -> None:
    contract = _write_project(tmp_path, name="atlas-service", version="2.4.1")
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract)

    assert verify_wheel.validate_wheel(wheel, contract) == []


def test_wheel_accepts_equivalent_python_specifier_order(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(
        wheel, contract, metadata=_metadata(contract, requires_python="<3.14,>=3.11")
    )

    assert verify_wheel.validate_wheel(wheel, contract) == []


@pytest.mark.parametrize(
    "unsafe_entry",
    [
        ("nexus" + "kernel") + "/__init__.py",
        "nexus_kernel/__pycache__/module.pyc",
        "app/.env.prod",
        "app/service-account.key",
        "app/credentials.json",
        "../outside.py",
    ],
)
def test_wheel_rejects_unsafe_entries(tmp_path: Path, unsafe_entry: str) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract, extra_entries=(unsafe_entry,))

    assert any(
        unsafe_entry in error for error in verify_wheel.validate_wheel(wheel, contract)
    )


def test_wheel_rejects_unknown_top_level_directory(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract, extra_entries=("unknown/__init__.py",))

    assert "unknown top-level entry" in "\n".join(
        verify_wheel.validate_wheel(wheel, contract)
    )


def test_wheel_rejects_duplicate_archive_entries(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract, duplicate_entry="app/__init__.py")

    assert "duplicate wheel entry" in "\n".join(
        verify_wheel.validate_wheel(wheel, contract)
    )


def test_wheel_rejects_crc_corruption(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(
        wheel, contract, extra_entries=("app/payload.txt",), compression=ZIP_STORED
    )
    wheel.write_bytes(
        wheel.read_bytes().replace(b"crc-payload-unique", b"crc-payload-broken", 1)
    )

    assert "CRC" in "\n".join(verify_wheel.validate_wheel(wheel, contract))


@pytest.mark.parametrize("missing", ["METADATA", "WHEEL", "RECORD"])
def test_wheel_requires_dist_info_contract_files(tmp_path: Path, missing: str) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(wheel, contract, omitted_dist_info=frozenset({missing}))

    assert missing in "\n".join(verify_wheel.validate_wheel(wheel, contract))


@pytest.mark.parametrize(
    ("filename_name", "filename_version", "expected"),
    [
        ("other-project", None, "wheel filename distribution"),
        (None, "9.9.9", "wheel filename version"),
    ],
)
def test_wheel_rejects_incorrect_filename_metadata(
    tmp_path: Path,
    filename_name: str | None,
    filename_version: str | None,
    expected: str,
) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(
        tmp_path,
        contract,
        filename_name=filename_name,
        filename_version=filename_version,
    )
    _write_wheel(wheel, contract)

    assert expected in "\n".join(verify_wheel.validate_wheel(wheel, contract))


def test_wheel_rejects_incorrect_python_metadata(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    wheel = _wheel_path(tmp_path, contract)
    _write_wheel(
        wheel, contract, metadata=_metadata(contract, requires_python=">=3.11,<3.12")
    )

    assert "Requires-Python" in "\n".join(verify_wheel.validate_wheel(wheel, contract))


def test_verifier_cli_requires_exactly_one_wheel(tmp_path: Path) -> None:
    contract = _write_project(tmp_path)
    _write_wheel(tmp_path / "first.whl", contract)
    _write_wheel(tmp_path / "second.whl", contract)

    result = subprocess.run(
        [sys.executable, str(VERIFIER), str(tmp_path), "--project-root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "exactly one wheel" in result.stderr
