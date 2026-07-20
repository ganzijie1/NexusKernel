"""Shared delivery constants for standalone project scripts."""

from __future__ import annotations

from pathlib import PurePath
from types import MappingProxyType
from typing import Final, Mapping


ENV_PROFILES: Final[tuple[str, ...]] = ("dev", "preprod", "prod")
ENV_EXAMPLE_TO_GENERATED: Final[Mapping[str, str]] = MappingProxyType(
    {f".env.{profile}.example": f".env.{profile}" for profile in ENV_PROFILES}
)
ENV_EXAMPLE_NAMES: Final[frozenset[str]] = frozenset(ENV_EXAMPLE_TO_GENERATED)
GENERATED_ENV_FILES: Final[frozenset[str]] = frozenset(
    f"docker/{name}" for name in ENV_EXAMPLE_TO_GENERATED.values()
)

SENSITIVE_SUFFIXES: Final[frozenset[str]] = frozenset({".key", ".pem", ".p12", ".pfx"})
SENSITIVE_NAMES: Final[frozenset[str]] = frozenset(
    {"credentials.json", "service-account.json"}
)


def is_sensitive_artifact(path: PurePath) -> bool:
    """Return whether an artifact is forbidden regardless of its contents."""

    return (
        path.name.casefold() in SENSITIVE_NAMES
        or path.suffix.casefold() in SENSITIVE_SUFFIXES
    )
