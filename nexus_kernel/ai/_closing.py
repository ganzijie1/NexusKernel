"""Cancellation-safe concurrent close coordination."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar


_TaskResult = TypeVar("_TaskResult")


class CallAdmissionError(RuntimeError):
    pass


def observe_task(task: asyncio.Task[_TaskResult]) -> asyncio.Task[_TaskResult]:
    """Ensure a detached task's exception can never reach the loop logger."""

    def consume(completed: asyncio.Task[Any]) -> None:
        try:
            completed.exception()
        except asyncio.CancelledError:
            pass

    task.add_done_callback(consume)
    return task


class CloseCoordinator:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def unavailable(self) -> bool:
        return self._closed or self._task is not None

    async def close(self, callback: Callable[[], Awaitable[None]]) -> None:
        if self._closed:
            return
        task = self._task
        if task is not None and task.done():
            if task.cancelled() or task.exception() is not None:
                self._task = None
                task = None
        if task is None:
            task = observe_task(asyncio.create_task(self._run(callback)))
            self._task = task
        await asyncio.shield(task)

    async def _run(self, callback: Callable[[], Awaitable[None]]) -> None:
        await callback()
        self._closed = True


class ActiveCallGate:
    """Linearize request admission with resource shutdown."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active = 0
        self._closing = False
        self._closed = False

    @property
    def unavailable(self) -> bool:
        return self._closing or self._closed

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        async with self._condition:
            if self._closing or self._closed:
                raise CallAdmissionError("AI resource is closing")
            self._active += 1
        try:
            yield
        finally:
            await self._release_admission()

    async def _release_admission(self) -> None:
        cancellation: asyncio.CancelledError | None = None
        acquired = False
        while not acquired:
            try:
                await self._condition.acquire()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                continue
            acquired = True
        try:
            self._active -= 1
            if self._active == 0:
                self._condition.notify_all()
        finally:
            self._condition.release()
        if cancellation is not None:
            raise cancellation

    async def drain(self, timeout_seconds: float) -> None:
        async with self._condition:
            self._closing = True
            try:
                async with asyncio.timeout(timeout_seconds):
                    await self._condition.wait_for(lambda: self._active == 0)
            except TimeoutError:
                raise TimeoutError("active AI calls did not finish") from None

    def mark_closed(self) -> None:
        self._closed = True
