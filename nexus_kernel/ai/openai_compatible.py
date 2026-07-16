"""OpenAI SDK adapter for OpenAI-compatible chat completion endpoints."""

from __future__ import annotations

from typing import Any, Protocol, cast

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
            max_tool_result_bytes=max_tool_result_bytes,
        )
        if client is None:
            from openai import AsyncOpenAI

            client = cast(
                OpenAIClientProtocol,
                AsyncOpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    timeout=timeout_seconds,
                ),
            )
        self._client: OpenAIClientProtocol = client

    async def _complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
        return await self._client.chat.completions.create(**kwargs)

    async def close(self) -> None:
        await self._coordinate_close(self._client.close)
