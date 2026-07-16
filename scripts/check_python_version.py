#!/usr/bin/env python3
"""Validate the interpreter range supported by this project."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


MIN_VERSION = (3, 11)
MAX_VERSION = (3, 14)
VERSION_SPEC = ">=3.11,<3.14"


def is_supported(version: Sequence[int]) -> bool:
    current = tuple(version[:2])
    return MIN_VERSION <= current < MAX_VERSION


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    version = sys.version_info[:3]
    supported = is_supported(version)

    if not arguments.quiet:
        message = (
            f"Python {version[0]}.{version[1]}.{version[2]} "
            f"is {'supported' if supported else 'unsupported'}; "
            f"required range is {VERSION_SPEC}."
        )
        print(message, file=sys.stdout if supported else sys.stderr)
    return 0 if supported else 1


if __name__ == "__main__":
    raise SystemExit(main())
