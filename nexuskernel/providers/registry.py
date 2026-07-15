"""Ordered Provider registry with rollback, degradation, and safe status output."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from nexuskernel.errors import ProviderError, ProviderUnavailableError
from nexuskernel.kernel.module import ProviderState
from nexuskernel.providers.base import Provider, ProviderSpec


@dataclass(slots=True)
class _Entry:
    spec: ProviderSpec
    state: ProviderState
    instance: Provider | None = None
    error: str | None = None


class ProviderRegistry:
    """Owns Provider creation, health checks, and deterministic shutdown."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._active: list[_Entry] = []
        self._closed = False

    def register(self, spec: ProviderSpec) -> None:
        if spec.name in self._entries:
            raise ProviderError(f"Provider is already registered: {spec.name}")
        state = ProviderState.REGISTERED if spec.enabled else ProviderState.DISABLED
        self._entries[spec.name] = _Entry(spec=spec, state=state)

    async def initialize_all(self) -> None:
        if self._closed:
            raise ProviderError("Provider registry is closed")

        for entry in self._entries.values():
            if entry.state is not ProviderState.REGISTERED:
                continue
            try:
                await self._initialize(entry)
            except asyncio.CancelledError:
                entry.state = ProviderState.FAILED
                entry.error = "provider_initialization_cancelled"
                await self._close_failed_instance(entry)
                await self._rollback_active()
                raise
            except Exception as exc:
                entry.state = (
                    ProviderState.FAILED
                    if entry.spec.required
                    else ProviderState.DEGRADED
                )
                entry.error = "provider_initialization_failed"
                await self._close_failed_instance(entry)
                if entry.spec.required:
                    await self._rollback_active()
                    raise ProviderUnavailableError(
                        f"Required provider is unavailable: {entry.spec.name}"
                    ) from exc

    async def _initialize(self, entry: _Entry) -> None:
        entry.state = ProviderState.STARTING
        created = entry.spec.factory()
        if inspect.isawaitable(created):
            created = await created
        instance = created
        self._validate_instance(instance)
        entry.instance = instance
        await asyncio.wait_for(instance.start(), timeout=entry.spec.timeout_seconds)
        entry.state = ProviderState.READY
        entry.error = None
        self._active.append(entry)

    @staticmethod
    def _validate_instance(instance: Any) -> None:
        required_methods = ("start", "health_check", "close")
        if any(
            not callable(getattr(instance, name, None)) for name in required_methods
        ):
            raise TypeError("provider does not implement the lifecycle contract")

    async def _close_failed_instance(self, entry: _Entry) -> None:
        if entry.instance is None:
            return
        try:
            await asyncio.wait_for(
                entry.instance.close(), timeout=entry.spec.timeout_seconds
            )
        except Exception:
            pass
        finally:
            entry.instance = None

    async def _rollback_active(self) -> None:
        for entry in reversed(self._active):
            await self._close_entry(entry)
        self._active.clear()

    async def refresh_health(self) -> list[dict[str, Any]]:
        for entry in list(self._active):
            if entry.instance is None:
                continue
            try:
                healthy = await asyncio.wait_for(
                    entry.instance.health_check(),
                    timeout=entry.spec.timeout_seconds,
                )
            except TimeoutError:
                entry.state = ProviderState.DEGRADED
                entry.error = "provider_health_timeout"
            except Exception:
                entry.state = ProviderState.DEGRADED
                entry.error = "provider_health_failed"
            else:
                entry.state = ProviderState.READY if healthy else ProviderState.DEGRADED
                entry.error = None if healthy else "provider_unhealthy"
        return self.statuses()

    def get(self, name: str) -> Provider | None:
        entry = self._entries.get(name)
        if entry is None:
            return None
        return entry.instance

    def require(self, name: str) -> Provider:
        provider = self.get(name)
        if provider is None:
            raise ProviderUnavailableError(f"Provider is unavailable: {name}")
        return provider

    def statuses(self) -> list[dict[str, Any]]:
        return [
            {
                "name": entry.spec.name,
                "required": entry.spec.required,
                "state": entry.state.value,
                "error": entry.error,
            }
            for entry in self._entries.values()
        ]

    async def close_all(self) -> list[str]:
        if self._closed:
            return []
        errors: list[str] = []
        for entry in reversed(self._active):
            if not await self._close_entry(entry):
                errors.append(f"provider_close_failed:{entry.spec.name}")
        self._active.clear()
        self._closed = True
        return errors

    async def _close_entry(self, entry: _Entry) -> bool:
        if entry.instance is None:
            return True
        succeeded = True
        try:
            await asyncio.wait_for(
                entry.instance.close(), timeout=entry.spec.timeout_seconds
            )
        except Exception:
            entry.state = ProviderState.FAILED
            entry.error = "provider_close_failed"
            succeeded = False
        else:
            entry.state = ProviderState.STOPPED
            entry.error = None
        finally:
            entry.instance = None
        return succeeded
