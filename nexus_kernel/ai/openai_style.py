"""Shared OpenAI-shaped completion parsing and bounded server tool execution."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from nexus_kernel.ai._closing import CloseCoordinator
from nexus_kernel.ai.contracts import (
    AgentToolSpec,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderId,
    TokenUsage,
    thaw_json,
)
from nexus_kernel.ai.errors import AiProviderError, AiTimeoutError, AiToolError
from nexus_kernel.ai.tool_executor import ToolExecutor


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _usage_value(usage: Any, name: str) -> int:
    value = _read(usage, name, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AiProviderError("AI provider returned invalid token usage")
    return value


class OpenAIStyleClient(ABC):
    provider_id: ProviderId

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float = 25.0,
        tool_timeout_seconds: float = 10.0,
        max_tool_rounds: int = 4,
        max_tool_calls: int = 16,
        max_tool_result_bytes: int = 65_536,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be positive")
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if max_tool_result_bytes < 1:
            raise ValueError("max_tool_result_bytes must be positive")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.tool_timeout_seconds = tool_timeout_seconds
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls = max_tool_calls
        self.max_tool_result_bytes = max_tool_result_bytes
        self._healthy = True
        self._close_coordinator = CloseCoordinator()

    @property
    def _closed(self) -> bool:
        return self._close_coordinator.unavailable

    async def start(self) -> None:
        if self._closed:
            raise AiProviderError("AI client is closed")

    async def health_check(self) -> bool:
        return not self._closed and self._healthy

    async def chat(
        self,
        request: ChatRequest,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse:
        if self._closed:
            raise AiProviderError("AI client is closed")
        executor = ToolExecutor(
            tools,
            timeout_seconds=self.tool_timeout_seconds,
            max_calls=self.max_tool_calls,
            max_result_bytes=self.max_tool_result_bytes,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._chat(request, executor)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._healthy = False
            raise AiTimeoutError("AI provider request timed out") from None
        except (AiProviderError, AiTimeoutError):
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("AI provider request failed") from None

    async def _chat(self, request: ChatRequest, executor: ToolExecutor) -> ChatResponse:
        messages = [self._message_payload(message) for message in request.messages]
        tool_payloads = [self._tool_payload(tool) for tool in executor.tools]
        input_tokens = output_tokens = total_tokens = 0

        for round_index in range(self.max_tool_rounds + 1):
            response = await self._safe_complete(messages, tool_payloads)
            try:
                usage = _read(response, "usage", {})
                input_tokens += _usage_value(usage, "prompt_tokens")
                output_tokens += _usage_value(usage, "completion_tokens")
                total_tokens += _usage_value(usage, "total_tokens")
                choices = _read(response, "choices", [])
                if not isinstance(choices, Sequence) or not choices:
                    raise AiProviderError("AI provider returned no completion choice")
                choice = choices[0]
                provider_message = _read(choice, "message", {})
                tool_calls = list(_read(provider_message, "tool_calls", []) or [])
                if tool_calls:
                    if round_index >= self.max_tool_rounds:
                        raise AiProviderError("AI tool-call round limit exceeded")
                    messages.append(
                        self._assistant_tool_message(provider_message, tool_calls)
                    )
                    messages.extend(await self._execute_tools(tool_calls, executor))
                    continue
                content = _read(provider_message, "content")
                if not isinstance(content, str) or not content.strip():
                    raise AiProviderError("AI provider returned no text content")
                model = _read(response, "model", self.model)
                if not isinstance(model, str) or not model.strip():
                    raise AiProviderError("AI provider returned an invalid model")
                finish_reason = _read(choice, "finish_reason")
                if finish_reason is not None and not isinstance(finish_reason, str):
                    raise AiProviderError(
                        "AI provider returned an invalid finish reason"
                    )
                normalized_total = max(total_tokens, input_tokens + output_tokens)
                return ChatResponse(
                    message=ChatMessage(role="assistant", content=content),
                    usage=TokenUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=normalized_total,
                    ),
                    model=model,
                    provider_id=self.provider_id,
                    finish_reason=finish_reason,
                )
            except (AiTimeoutError, AiToolError):
                raise
            except AiProviderError:
                self._healthy = False
                raise
            except Exception:
                self._healthy = False
                raise AiProviderError(
                    "AI provider returned an invalid response"
                ) from None
        raise AiProviderError("AI tool-call round limit exceeded")

    async def _safe_complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        try:
            response = await self._complete(messages, tools)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._healthy = False
            raise AiTimeoutError("AI provider request timed out") from None
        except AiTimeoutError:
            self._healthy = False
            raise
        except AiProviderError:
            self._healthy = False
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("AI provider request failed") from None
        self._healthy = True
        return response

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
                "parameters": thaw_json(tool.parameters),
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
    async def _execute_tools(
        calls: list[Any], executor: ToolExecutor
    ) -> list[dict[str, Any]]:
        parsed: list[tuple[str, str, dict[str, Any]]] = []
        for call in calls:
            function = _read(call, "function", {})
            name = _read(function, "name", "")
            call_id = _read(call, "id", "")
            if not isinstance(name, str) or not name:
                raise AiToolError("AI provider requested an invalid tool")
            if not isinstance(call_id, str) or not call_id:
                raise AiToolError("AI provider returned an invalid tool call")
            try:
                arguments = json.loads(str(_read(function, "arguments", "{}")))
            except (TypeError, ValueError):
                raise AiToolError(
                    "AI provider supplied invalid tool arguments"
                ) from None
            if not isinstance(arguments, dict):
                raise AiToolError("AI provider supplied invalid tool arguments")
            parsed.append((call_id, name, arguments))
        serialized = await executor.execute_json_batch(
            [(name, arguments) for _, name, arguments in parsed]
        )
        return [
            {"role": "tool", "tool_call_id": call_id, "content": content}
            for (call_id, _, _), content in zip(parsed, serialized, strict=True)
        ]

    async def _coordinate_close(self, callback: Callable[[], Awaitable[None]]) -> None:
        try:
            await self._close_coordinator.close(callback)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("AI client close failed") from None

    @abstractmethod
    async def close(self) -> None: ...
