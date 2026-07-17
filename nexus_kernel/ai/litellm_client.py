"""LiteLLM adapter using only public completion and close functions."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from typing import Any, cast

from nexus_kernel.ai._closing import observe_task
from nexus_kernel.ai.errors import AiProviderError
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
        max_tool_argument_bytes: int = 65_536,
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
            max_tool_argument_bytes=max_tool_argument_bytes,
            max_tool_result_bytes=max_tool_result_bytes,
        )
        self._completion = completion
        self._close_clients = close_clients
        self._start_task: (
            asyncio.Task[tuple[CompletionCallable, CloseCallable]] | None
        ) = None
        self._api_key = api_key
        self._base_url = base_url

    @staticmethod
    def _load_default_callables() -> tuple[CompletionCallable, CloseCallable]:
        if os.getenv("LITELLM_LOCAL_MODEL_COST_MAP", "").lower() != "true":
            raise RuntimeError("LiteLLM local model cost map is required")
        import litellm

        return (
            cast(CompletionCallable, litellm.acompletion),
            cast(
                CloseCallable,
                getattr(litellm, "close_litellm_async_clients"),
            ),
        )

    async def start(self) -> None:
        await super().start()
        if self._completion is not None and self._close_clients is not None:
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
                asyncio.create_task(asyncio.to_thread(self._load_default_callables))
            )
            self._start_task = task
        try:
            default_completion, default_close = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("LiteLLM initialization failed") from None
        self._completion = self._completion or default_completion
        self._close_clients = self._close_clients or default_close
        if self._closed:
            raise AiProviderError("AI client is closed")

    async def _complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        completion = self._completion
        if completion is None:
            raise AiProviderError("LiteLLM client is unavailable")
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
        return await completion(**kwargs)

    async def close(self) -> None:
        async def close_clients() -> None:
            close_callable = self._close_clients
            task = self._start_task
            if close_callable is None and task is not None:
                try:
                    _, close_callable = await asyncio.shield(task)
                except Exception:
                    close_callable = None
                else:
                    self._close_clients = close_callable
            if close_callable is None:
                return
            result = close_callable()
            if inspect.isawaitable(result):
                await result

        await self._coordinate_close(close_clients)
