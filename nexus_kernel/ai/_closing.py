"""Cancellation-safe concurrent close coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


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
            task = asyncio.create_task(self._run(callback))
            self._task = task
        await asyncio.shield(task)

    async def _run(self, callback: Callable[[], Awaitable[None]]) -> None:
        await callback()
        self._closed = True
