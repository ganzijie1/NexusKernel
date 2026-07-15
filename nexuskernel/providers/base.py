"""Provider lifecycle contracts used by framework-managed integrations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeAlias


class Provider(Protocol):
    """Minimal lifecycle implemented by database, cache, AI, and tracing providers."""

    async def start(self) -> None: ...

    async def health_check(self) -> bool: ...

    async def close(self) -> None: ...


ProviderFactory: TypeAlias = Callable[[], Provider | Awaitable[Provider]]


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    factory: ProviderFactory
    required: bool = True
    enabled: bool = True
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        import re

        if re.fullmatch(r"[a-z][a-z0-9_]*", self.name) is None:
            raise ValueError("provider name must be a stable lowercase identifier")
        if self.timeout_seconds <= 0:
            raise ValueError("provider timeout_seconds must be positive")
