from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/check_python_version.py"


def _load_checker() -> ModuleType:
    assert CHECKER.is_file(), "missing scripts/check_python_version.py"
    spec = importlib.util.spec_from_file_location("check_python_version", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ((3, 10, 99), False),
        ((3, 11, 0), True),
        ((3, 12, 9), True),
        ((3, 13, 99), True),
        ((3, 14, 0), False),
        ((4, 0, 0), False),
    ],
)
def test_supported_python_range_uses_numeric_comparison(
    version: tuple[int, int, int], supported: bool
) -> None:
    checker = _load_checker()

    assert checker.is_supported(version) is supported


def test_checker_cli_accepts_current_supported_interpreter() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Python" in result.stdout
    assert ">=3.11,<3.14" in result.stdout


def test_checker_quiet_mode_has_no_output() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
