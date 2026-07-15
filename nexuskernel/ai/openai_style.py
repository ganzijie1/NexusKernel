"""Shared OpenAI-shaped completion parsing and server tool execution."""

from __future__ import annotations

import asyncio
import inspect
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from nexuskernel.ai.contracts import (
    AgentToolSpec,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderId,
    TokenUsage,
)
from nexuskernel.ai.errors import AiProviderError, AiTimeoutError


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class OpenAIStyleClient(ABC):
    provider_id: ProviderId

    def __init__(
        self, *, model: str, timeout_seconds: float = 30.0, max_tool_rounds: int = 4
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be positive")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tool_rounds = max_tool_rounds
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise AiProviderError("AI client is closed")

    async def health_check(self) -> bool:
        return not self._closed

    async def chat(
        self,
        request: ChatRequest,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse:
        if self._closed:
            raise AiProviderError("AI client is closed")
        messages = [self._message_payload(message) for message in request.messages]
        tool_map = {tool.name: tool for tool in tools}
        tool_payloads = [self._tool_payload(tool) for tool in tools]
        input_tokens = output_tokens = total_tokens = 0

        for _ in range(self.max_tool_rounds + 1):
            response = await self._safe_complete(messages, tool_payloads)
            usage = _read(response, "usage", {})
            input_tokens += int(_read(usage, "prompt_tokens", 0) or 0)
            output_tokens += int(_read(usage, "completion_tokens", 0) or 0)
            total_tokens += int(_read(usage, "total_tokens", 0) or 0)
            choices = _read(response, "choices", [])
            if not choices:
                raise AiProviderError("AI provider returned no completion choice")
            choice = choices[0]
            provider_message = _read(choice, "message", {})
            tool_calls = list(_read(provider_message, "tool_calls", []) or [])
            if tool_calls:
                messages.append(
                    self._assistant_tool_message(provider_message, tool_calls)
                )
                for call in tool_calls:
                    messages.append(await self._execute_tool(call, tool_map))
                continue
            content = _read(provider_message, "content")
            if not isinstance(content, str) or not content:
                raise AiProviderError("AI provider returned no text content")
            normalized_total = max(total_tokens, input_tokens + output_tokens)
            return ChatResponse(
                message=ChatMessage(role="assistant", content=content),
                usage=TokenUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=normalized_total,
                ),
                model=str(_read(response, "model", self.model)),
                provider_id=self.provider_id,
                finish_reason=_read(choice, "finish_reason"),
            )
        raise AiProviderError("AI tool-call limit exceeded")

    async def _safe_complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        try:
            return await asyncio.wait_for(
                self._complete(messages, tools), timeout=self.timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise AiTimeoutError("AI provider request timed out") from exc
        except AiProviderError:
            raise
        except Exception:
            raise AiProviderError("AI provider request failed") from None

    @abstractmethod
    async def _complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any: ...

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.name is not None:
            payload["name"] = message.name
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    @staticmethod
    def _tool_payload(tool: AgentToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            },
        }

    @staticmethod
    def _assistant_tool_message(
        provider_message: Any, tool_calls: list[Any]
    ) -> dict[str, Any]:
        normalized = []
        for call in tool_calls:
            function = _read(call, "function", {})
            normalized.append(
                {
                    "id": str(_read(call, "id", "")),
                    "type": "function",
                    "function": {
                        "name": str(_read(function, "name", "")),
                        "arguments": str(_read(function, "arguments", "{}")),
                    },
                }
            )
        return {
            "role": "assistant",
            "content": _read(provider_message, "content"),
            "tool_calls": normalized,
        }

    @staticmethod
    async def _execute_tool(
        call: Any, tool_map: dict[str, AgentToolSpec]
    ) -> dict[str, Any]:
        function = _read(call, "function", {})
        name = str(_read(function, "name", ""))
        tool = tool_map.get(name)
        if tool is None:
            raise AiProviderError("AI provider requested an unregistered tool")
        try:
            arguments = json.loads(str(_read(function, "arguments", "{}")))
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            result = tool.handler(arguments)
            if inspect.isawaitable(result):
                result = await result
            serialized = json.dumps(result, ensure_ascii=True, default=str)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AiProviderError("Server tool execution failed") from None
        return {
            "role": "tool",
            "tool_call_id": str(_read(call, "id", "")),
            "content": serialized,
        }

    @abstractmethod
    async def close(self) -> None: ...
