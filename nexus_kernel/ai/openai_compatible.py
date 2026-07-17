"""OpenAI SDK adapter for OpenAI-compatible chat completion endpoints."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast

from nexus_kernel.ai._closing import observe_task
from nexus_kernel.ai.errors import AiProviderError
from nexus_kernel.ai.openai_style import OpenAIStyleClient


class _Completions(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class _Chat(Protocol):
    completions: _Completions


class OpenAIClientProtocol(Protocol):
    chat: _Chat

    async def close(self) -> None: ...


class OpenAICompatibleClient(OpenAIStyleClient):
    provider_id = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 25.0,
        tool_timeout_seconds: float = 10.0,
        max_tool_rounds: int = 4,
        max_tool_calls: int = 16,
        max_tool_argument_bytes: int = 65_536,
        max_tool_result_bytes: int = 65_536,
        client: OpenAIClientProtocol | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        super().__init__(
            model=model,
            timeout_seconds=timeout_seconds,
            tool_timeout_seconds=tool_timeout_seconds,
            max_tool_rounds=max_tool_rounds,
            max_tool_calls=max_tool_calls,
            max_tool_argument_bytes=max_tool_argument_bytes,
            max_tool_result_bytes=max_tool_result_bytes,
        )
        self._api_key = api_key
        self._base_url = base_url
        self._client: OpenAIClientProtocol | None = client
        self._start_task: asyncio.Task[OpenAIClientProtocol] | None = None

    def _create_default_client(self) -> OpenAIClientProtocol:
        from openai import AsyncOpenAI

        return cast(
            OpenAIClientProtocol,
            AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self.timeout_seconds,
            ),
        )

    async def start(self) -> None:
        await super().start()
        if self._client is not None:
            return
        task = self._start_task
        if (
            task is not None
            and task.done()
            and (task.cancelled() or task.exception() is not None)
        ):
            task = None
            self._start_task = None
        if task is None:
            task = observe_task(
                asyncio.create_task(asyncio.to_thread(self._create_default_client))
            )
            self._start_task = task
        try:
            client = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("OpenAI client initialization failed") from None
        self._client = client
        if self._closed:
            raise AiProviderError("AI client is closed")

    async def _complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        client = self._client
        if client is None:
            raise AiProviderError("OpenAI client is unavailable")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
        return await client.chat.completions.create(**kwargs)

    async def close(self) -> None:
        async def close_client() -> None:
            client = self._client
            task = self._start_task
            if client is None and task is not None:
                try:
                    client = await asyncio.shield(task)
                except Exception:
                    client = None
                else:
                    self._client = client
            if client is not None:
                await client.close()

        await self._coordinate_close(close_client)
