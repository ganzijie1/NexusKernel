"""Ordered Provider registry with rollback, degradation, and safe status output."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from nexus_kernel.errors import ProviderError, ProviderUnavailableError
from nexus_kernel.providers.base import Provider, ProviderSpec, ProviderState


@dataclass(slots=True)
class _Entry:
    spec: ProviderSpec
    state: ProviderState
    instance: Provider | None = None
    candidate_created: bool = False
    error: str | None = None
    cleanup_task: asyncio.Task[None] | None = None


class ProviderRegistry:
    """Owns Provider creation, health checks, and deterministic shutdown."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._active: list[_Entry] = []
        self._closed = False
        self._initialized = False
        self._initialization_failed = False
        self._initializing = False
        self._lifecycle_lock = asyncio.Lock()

    def register(self, spec: ProviderSpec) -> None:
        if (
            self._closed
            or self._initialized
            or self._initialization_failed
            or self._initializing
        ):
            raise ProviderError("Providers cannot be registered after initialization")
        if spec.name in self._entries:
            raise ProviderError(f"Provider is already registered: {spec.name}")
        state = ProviderState.REGISTERED if spec.enabled else ProviderState.DISABLED
        self._entries[spec.name] = _Entry(spec=spec, state=state)

    async def initialize_all(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise ProviderError("Provider registry is closed")
            if self._initialized:
                return
            if self._initialization_failed:
                raise ProviderUnavailableError(
                    "Provider registry initialization previously failed"
                ) from None

            self._initializing = True
            try:
                await self._initialize_all_once()
            except asyncio.CancelledError:
                self._initialization_failed = True
                raise
            except Exception:
                self._initialization_failed = True
                raise
            else:
                self._initialized = True
            finally:
                self._initializing = False

    async def _initialize_all_once(self) -> None:
        for entry in self._entries.values():
            if entry.state is not ProviderState.REGISTERED:
                continue
            try:
                await self._initialize(entry)
            except asyncio.CancelledError as cancellation:
                entry.state = ProviderState.FAILED
                entry.error = "provider_initialization_cancelled"
                await self._close_failed_instance(entry)
                await self._rollback_active()
                raise cancellation
            except TimeoutError:
                entry.state = ProviderState.FAILED
                entry.error = "provider_initialization_timeout"
                cleanup_cancellation = await self._close_failed_instance(entry)
                if entry.spec.required:
                    rollback_cancellation = await self._rollback_active()
                    deferred_cancellation = (
                        cleanup_cancellation or rollback_cancellation
                    )
                    if deferred_cancellation is not None:
                        raise deferred_cancellation
                    raise ProviderUnavailableError(
                        f"Required provider is unavailable: {entry.spec.name}"
                    ) from None
                if cleanup_cancellation is not None:
                    await self._rollback_active()
                    raise cleanup_cancellation
            except Exception:
                entry.state = ProviderState.FAILED
                entry.error = "provider_initialization_failed"
                cleanup_cancellation = await self._close_failed_instance(entry)
                if entry.spec.required:
                    rollback_cancellation = await self._rollback_active()
                    deferred_cancellation = (
                        cleanup_cancellation or rollback_cancellation
                    )
                    if deferred_cancellation is not None:
                        raise deferred_cancellation
                    raise ProviderUnavailableError(
                        f"Required provider is unavailable: {entry.spec.name}"
                    ) from None
                if cleanup_cancellation is not None:
                    await self._rollback_active()
                    raise cleanup_cancellation

    async def _initialize(self, entry: _Entry) -> None:
        entry.state = ProviderState.STARTING
        # Sync factories are required to be non-blocking; async work shares this deadline.
        async with asyncio.timeout(entry.spec.timeout_seconds):
            created = entry.spec.factory()
            if inspect.isawaitable(created):
                created = await created
            instance = created
            entry.candidate_created = True
            entry.instance = instance
            self._validate_instance(instance)
            await instance.start()
        entry.state = ProviderState.READY
        entry.error = None
        self._active.append(entry)

    @staticmethod
    def _validate_instance(instance: Any) -> None:
        for name in ("start", "health_check", "close"):
            method = getattr(instance, name, None)
            if not callable(method) or not inspect.iscoroutinefunction(method):
                raise TypeError("provider does not implement the lifecycle contract")
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                raise TypeError(
                    "provider does not implement the lifecycle contract"
                ) from None
            if parameters:
                raise TypeError("provider does not implement the lifecycle contract")

    @staticmethod
    async def _invoke_close(close: Any) -> None:
        result = close()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _consume_cleanup_result(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    async def _wait_for_cleanup(
        task: asyncio.Task[None], timeout_seconds: float
    ) -> tuple[bool, asyncio.CancelledError | None]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                done, _ = await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                continue
            if not done:
                break

        if not task.done():
            task.cancel()
            task.add_done_callback(ProviderRegistry._consume_cleanup_result)
            return False, cancellation
        try:
            task.result()
        except BaseException:
            return False, cancellation
        return True, cancellation

    def _ensure_cleanup_task(self, entry: _Entry) -> asyncio.Task[None] | None:
        if entry.cleanup_task is not None:
            return entry.cleanup_task
        if not entry.candidate_created:
            return None
        if entry.instance is None:
            entry.error = "provider_cleanup_unavailable"
            return None
        try:
            close = getattr(entry.instance, "close", None)
        except Exception:
            return None
        if not callable(close) or not inspect.iscoroutinefunction(close):
            return None
        try:
            parameters = inspect.signature(close).parameters
        except (TypeError, ValueError):
            return None
        if parameters:
            return None
        entry.cleanup_task = asyncio.create_task(
            self._invoke_close(close),
            name=f"nexus_kernel_provider_cleanup_{entry.spec.name}",
        )
        return entry.cleanup_task

    async def _close_failed_instance(
        self, entry: _Entry
    ) -> asyncio.CancelledError | None:
        if not entry.candidate_created:
            return None
        if entry.instance is None:
            entry.error = "provider_cleanup_unavailable"
            return None
        task = self._ensure_cleanup_task(entry)
        if task is None:
            entry.error = "provider_cleanup_unavailable"
            entry.instance = None
            return None
        _, cancellation = await self._wait_for_cleanup(task, entry.spec.timeout_seconds)
        entry.instance = None
        return cancellation

    async def _rollback_active(self) -> asyncio.CancelledError | None:
        cancellation: asyncio.CancelledError | None = None
        for entry in reversed(self._active):
            _, close_cancellation = await self._close_entry(entry)
            cancellation = cancellation or close_cancellation
        self._active.clear()
        return cancellation

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
        async with self._lifecycle_lock:
            if self._closed:
                return []
            errors: list[str] = []
            cancellation: asyncio.CancelledError | None = None
            for entry in reversed(self._active):
                succeeded, close_cancellation = await self._close_entry(entry)
                cancellation = cancellation or close_cancellation
                if not succeeded:
                    error = entry.error or "provider_close_failed"
                    errors.append(f"{error}:{entry.spec.name}")
            self._active.clear()
            self._closed = True
            if cancellation is not None:
                raise cancellation
            return errors

    async def _close_entry(
        self, entry: _Entry
    ) -> tuple[bool, asyncio.CancelledError | None]:
        if entry.instance is None:
            return True, None
        task = self._ensure_cleanup_task(entry)
        if task is None:
            succeeded = False
            cancellation = None
            entry.error = "provider_cleanup_unavailable"
        else:
            succeeded, cancellation = await self._wait_for_cleanup(
                task, entry.spec.timeout_seconds
            )
        if not succeeded:
            entry.state = ProviderState.FAILED
            if entry.error != "provider_cleanup_unavailable":
                entry.error = "provider_close_failed"
        else:
            entry.state = ProviderState.STOPPED
            entry.error = None
        entry.instance = None
        return succeeded, cancellation
