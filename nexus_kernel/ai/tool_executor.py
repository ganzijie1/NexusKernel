"""Bounded execution for application-owned server tools."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import ValidationError  # type: ignore[import-untyped]
from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]

from nexus_kernel.ai.contracts import AgentToolSpec, thaw_json
from nexus_kernel.ai.errors import AiTimeoutError, AiToolError


class ToolExecutor:
    def __init__(
        self,
        tools: Sequence[AgentToolSpec] = (),
        *,
        timeout_seconds: float = 10.0,
        max_calls: int = 16,
        max_result_bytes: int = 65_536,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")
        if max_calls < 1:
            raise ValueError("max tool calls must be positive")
        if max_result_bytes < 1:
            raise ValueError("max tool result bytes must be positive")
        tool_map = {tool.name: tool for tool in tools}
        if len(tool_map) != len(tools):
            raise ValueError("tool names must be unique")
        self._tools = tool_map
        self._timeout_seconds = timeout_seconds
        self._max_calls = max_calls
        self._max_result_bytes = max_result_bytes
        self._calls = 0
        self._budget_lock = asyncio.Lock()

    @property
    def tools(self) -> tuple[AgentToolSpec, ...]:
        return tuple(self._tools.values())

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> Any:
        tool, validated = self._validate_call(name, arguments)
        await self._reserve(1)
        return await self._execute_validated(tool, validated)

    async def execute_batch(
        self, calls: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> list[Any]:
        validated = [self._validate_call(name, arguments) for name, arguments in calls]
        await self._reserve(len(validated))
        results: list[Any] = []
        for tool, arguments in validated:
            results.append(await self._execute_validated(tool, arguments))
        return results

    async def _reserve(self, count: int) -> None:
        if count < 1:
            return
        async with self._budget_lock:
            if count > self._max_calls - self._calls:
                raise AiToolError("AI tool-call budget exceeded")
            self._calls += count

    def _validate_call(
        self, name: str, arguments: Mapping[str, Any]
    ) -> tuple[AgentToolSpec, dict[str, Any]]:
        tool = self._tools.get(name)
        if tool is None:
            raise AiToolError("AI provider requested an unregistered tool")
        validated = dict(arguments)
        try:
            Draft202012Validator(thaw_json(tool.parameters)).validate(validated)
        except ValidationError:
            raise AiToolError("AI provider supplied invalid tool arguments") from None
        return tool, validated

    async def _execute_validated(
        self, tool: AgentToolSpec, arguments: dict[str, Any]
    ) -> Any:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await self._invoke(tool, arguments)
            serialized = json.dumps(
                result,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise AiTimeoutError("Server tool execution timed out") from None
        except Exception:
            raise AiToolError("Server tool execution failed") from None
        if len(serialized.encode("utf-8")) > self._max_result_bytes:
            raise AiToolError("Server tool result exceeded the size limit")
        return json.loads(serialized)

    async def execute_json(self, name: str, arguments: Mapping[str, Any]) -> str:
        result = await self.execute(name, arguments)
        return json.dumps(
            result,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )

    async def execute_json_batch(
        self, calls: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> list[str]:
        results = await self.execute_batch(calls)
        return [
            json.dumps(
                result,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            for result in results
        ]

    @staticmethod
    async def _invoke(tool: AgentToolSpec, arguments: dict[str, Any]) -> Any:
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(arguments)
        result = await asyncio.to_thread(tool.handler, arguments)
        if inspect.isawaitable(result):
            return await result
        return result
