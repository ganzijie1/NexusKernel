"""LiteLLM adapter using only public completion and close functions."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from nexus_kernel.ai.openai_style import OpenAIStyleClient


CompletionCallable = Callable[..., Awaitable[Any]]
CloseCallable = Callable[[], Any]


class LiteLLMClient(OpenAIStyleClient):
    provider_id = "litellm"

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
        max_tool_result_bytes: int = 65_536,
        completion: CompletionCallable | None = None,
        close_clients: CloseCallable | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        super().__init__(
            model=model,
            timeout_seconds=timeout_seconds,
            tool_timeout_seconds=tool_timeout_seconds,
            max_tool_rounds=max_tool_rounds,
            max_tool_calls=max_tool_calls,
            max_tool_result_bytes=max_tool_result_bytes,
        )
        if completion is None or close_clients is None:
            import litellm

            completion = completion or cast(CompletionCallable, litellm.acompletion)
            close_clients = close_clients or cast(
                CloseCallable,
                getattr(litellm, "close_litellm_async_clients"),
            )
        self._completion = completion
        self._close_clients = close_clients
        self._api_key = api_key
        self._base_url = base_url

    async def _complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self._api_key,
            "stream": False,
            "timeout": self.timeout_seconds,
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if tools:
            kwargs["tools"] = tools
        return await self._completion(**kwargs)

    async def close(self) -> None:
        async def close_clients() -> None:
            result = self._close_clients()
            if inspect.isawaitable(result):
                await result

        await self._coordinate_close(close_clients)
