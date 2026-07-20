"""Bounded execution for application-owned server tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import ValidationError  # type: ignore[import-untyped]
from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]

from nexus_kernel._validation import require_positive_finite_number
from nexus_kernel.ai.contracts import AgentIdentity, AgentToolSpec, thaw_json
from nexus_kernel.ai.errors import AiTimeoutError, AiToolError


_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 16_384


class _InvalidStrictJson(ValueError):
    pass


class _JsonSizeExceeded(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidStrictJson("JSON object keys must be unique")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> None:
    del value
    raise _InvalidStrictJson("JSON numbers must be finite")


def _parse_bounded_json_object(
    value: str, *, max_bytes: int, deadline: float
) -> dict[str, Any]:
    size = 0
    for index, character in enumerate(value):
        size += len(character.encode("utf-8"))
        if size > max_bytes:
            raise _JsonSizeExceeded
        if index % 128 == 0 and asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
        )
    except (TypeError, ValueError, RecursionError):
        raise _InvalidStrictJson("tool arguments must be strict JSON") from None
    if type(parsed) is not dict:  # noqa: E721
        raise _InvalidStrictJson("tool arguments must be an object")
    return parsed


@dataclass(slots=True)
class _JsonTraversal:
    deadline: float
    nodes: int = 0

    def visit(self, depth: int) -> None:
        if depth > _MAX_JSON_DEPTH:
            raise _InvalidStrictJson("JSON nesting is too deep")
        self.nodes += 1
        if self.nodes > _MAX_JSON_NODES:
            raise _InvalidStrictJson("JSON structure is too large")
        if asyncio.get_running_loop().time() >= self.deadline:
            raise TimeoutError


def _copy_strict_json(value: Any, traversal: _JsonTraversal, depth: int = 0) -> Any:
    traversal.visit(depth)
    if value is None or type(value) in {bool, str}:  # noqa: E721
        return value
    if type(value) is int:  # noqa: E721
        return value
    if type(value) is float:  # noqa: E721
        if not math.isfinite(value):
            raise _InvalidStrictJson("JSON numbers must be finite")
        return value
    if type(value) is list:  # noqa: E721
        return [_copy_strict_json(item, traversal, depth + 1) for item in value]
    if type(value) is dict:  # noqa: E721
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:  # noqa: E721
                raise _InvalidStrictJson("JSON object keys must be strings")
            copied[key] = _copy_strict_json(item, traversal, depth + 1)
        return copied
    raise _InvalidStrictJson("value is not strict JSON")


class _BoundedJsonEncoder:
    def __init__(self, *, max_bytes: int, deadline: float) -> None:
        self._max_bytes = max_bytes
        self._deadline = deadline
        self._parts: list[str] = []
        self._size = 0
        self._steps = 0

    def encode(self, value: Any) -> str:
        self._encode_value(value)
        if asyncio.get_running_loop().time() >= self._deadline:
            raise TimeoutError
        return "".join(self._parts)

    def _append(self, value: str) -> None:
        self._steps += 1
        if self._steps % 128 == 0:
            if asyncio.get_running_loop().time() >= self._deadline:
                raise TimeoutError
        size = len(value.encode("utf-8"))
        if self._size + size > self._max_bytes:
            raise _JsonSizeExceeded
        self._parts.append(value)
        self._size += size

    def _encode_value(self, value: Any) -> None:
        if value is None:
            self._append("null")
        elif type(value) is bool:  # noqa: E721
            self._append("true" if value else "false")
        elif type(value) is int:  # noqa: E721
            if value.bit_length() > self._max_bytes * 4:
                raise _JsonSizeExceeded
            self._append(str(value))
        elif type(value) is float:  # noqa: E721
            self._append(json.dumps(value, allow_nan=False, separators=(",", ":")))
        elif type(value) is str:  # noqa: E721
            self._append('"')
            for character in value:
                escaped = json.dumps(character, ensure_ascii=False)[1:-1]
                self._append(escaped)
            self._append('"')
        elif type(value) is list:  # noqa: E721
            self._append("[")
            for index, item in enumerate(value):
                if index:
                    self._append(",")
                self._encode_value(item)
            self._append("]")
        elif type(value) is dict:  # noqa: E721
            self._append("{")
            for index, (key, item) in enumerate(value.items()):
                if index:
                    self._append(",")
                self._encode_value(key)
                self._append(":")
                self._encode_value(item)
            self._append("}")
        else:  # pragma: no cover - defensive after strict copying
            raise _InvalidStrictJson("value is not strict JSON")


@dataclass(frozen=True, slots=True)
class _PreparedCall:
    tool: AgentToolSpec
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ToolOutput:
    value: Any
    serialized: str


class ToolExecutor:
    def __init__(
        self,
        tools: Sequence[AgentToolSpec] = (),
        *,
        identity: AgentIdentity,
        timeout_seconds: float = 10.0,
        max_calls: int = 16,
        max_argument_bytes: int = 65_536,
        max_result_bytes: int = 65_536,
    ) -> None:
        require_positive_finite_number(timeout_seconds, "tool timeout")
        if max_calls < 1:
            raise ValueError("max tool calls must be positive")
        if max_argument_bytes < 1:
            raise ValueError("max tool argument bytes must be positive")
        if max_result_bytes < 1:
            raise ValueError("max tool result bytes must be positive")
        tool_map = {tool.name: tool for tool in tools}
        if len(tool_map) != len(tools):
            raise ValueError("tool names must be unique")
        self._tools = tool_map
        self._identity = identity
        self._timeout_seconds = timeout_seconds
        self._max_calls = max_calls
        self._max_argument_bytes = max_argument_bytes
        self._max_result_bytes = max_result_bytes
        self._calls = 0
        self._budget_lock = asyncio.Lock()

    @property
    def tools(self) -> tuple[AgentToolSpec, ...]:
        return tuple(self._tools.values())

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> Any:
        return (await self._execute_many(((name, arguments),)))[0].value

    async def execute_batch(
        self, calls: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> list[Any]:
        return [output.value for output in await self._execute_many(calls)]

    async def execute_json(self, name: str, arguments: Mapping[str, Any]) -> str:
        return (await self._execute_many(((name, arguments),)))[0].serialized

    async def execute_json_batch(
        self, calls: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> list[str]:
        return [output.serialized for output in await self._execute_many(calls)]

    async def execute_json_text_batch(
        self, calls: Sequence[tuple[str, str]]
    ) -> list[str]:
        return [output.serialized for output in await self._execute_many(calls)]

    async def _execute_many(
        self, calls: Sequence[tuple[str, Mapping[str, Any] | str]]
    ) -> list[_ToolOutput]:
        if not calls:
            return []
        if len(calls) > self._max_calls - self._calls:
            raise AiToolError("AI tool-call budget exceeded")
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                prepared = await asyncio.gather(
                    *(
                        self._prepare_call(name, arguments, deadline)
                        for name, arguments in calls
                    )
                )
                await self._reserve(len(prepared))
                return list(
                    await asyncio.gather(
                        *(
                            self._invoke_and_normalize(call, deadline)
                            for call in prepared
                        )
                    )
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise AiTimeoutError("Server tool execution timed out") from None

    async def _prepare_call(
        self, name: str, arguments: Mapping[str, Any] | str, deadline: float
    ) -> _PreparedCall:
        tool = self._tools.get(name)
        if tool is None:
            raise AiToolError("AI provider requested an unregistered tool")
        try:
            source = (
                _parse_bounded_json_object(
                    arguments,
                    max_bytes=self._max_argument_bytes,
                    deadline=deadline,
                )
                if type(arguments) is str  # noqa: E721
                else arguments
            )
            copied = _copy_strict_json(source, _JsonTraversal(deadline))
            if type(copied) is not dict:  # noqa: E721
                raise _InvalidStrictJson("tool arguments must be an object")
            _BoundedJsonEncoder(
                max_bytes=self._max_argument_bytes, deadline=deadline
            ).encode(copied)
            await asyncio.to_thread(
                Draft202012Validator(thaw_json(tool.parameters)).validate, copied
            )
        except ValidationError:
            raise AiToolError("AI provider supplied invalid tool arguments") from None
        except _JsonSizeExceeded:
            raise AiToolError("AI tool arguments exceeded the size limit") from None
        except _InvalidStrictJson:
            raise AiToolError("AI provider supplied invalid tool arguments") from None
        return _PreparedCall(tool=tool, arguments=copied)

    async def _reserve(self, count: int) -> None:
        async with self._budget_lock:
            if count > self._max_calls - self._calls:
                raise AiToolError("AI tool-call budget exceeded")
            self._calls += count

    async def _invoke_and_normalize(
        self, call: _PreparedCall, deadline: float
    ) -> _ToolOutput:
        try:
            result = await self._invoke(call.tool, call.arguments, self._identity)
            normalized = _copy_strict_json(result, _JsonTraversal(deadline))
            serialized = _BoundedJsonEncoder(
                max_bytes=self._max_result_bytes, deadline=deadline
            ).encode(normalized)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise
        except _JsonSizeExceeded:
            raise AiToolError("Server tool result exceeded the size limit") from None
        except _InvalidStrictJson:
            raise AiToolError("Server tool result is not strict JSON") from None
        except (AiTimeoutError, AiToolError):
            raise
        except Exception:
            raise AiToolError("Server tool execution failed") from None
        return _ToolOutput(value=normalized, serialized=serialized)

    @staticmethod
    async def _invoke(
        tool: AgentToolSpec,
        arguments: dict[str, Any],
        identity: AgentIdentity,
    ) -> Any:
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(arguments, identity)
        result = await asyncio.to_thread(tool.handler, arguments, identity)
        if inspect.isawaitable(result):
            return await result
        return result
