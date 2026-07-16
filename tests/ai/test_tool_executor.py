from __future__ import annotations

import asyncio
import math
import threading
from typing import Any

import pytest

from nexus_kernel.ai import AgentToolSpec, AiTimeoutError, AiToolError
from nexus_kernel.ai.tool_executor import ToolExecutor


def _tool(handler: Any, *, parameters: dict[str, Any] | None = None) -> AgentToolSpec:
    return AgentToolSpec(
        name="lookup",
        description="Look up a value.",
        parameters=parameters
        or {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def test_tool_schema_is_validated_and_defensively_frozen() -> None:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
    }
    tool = _tool(lambda arguments: arguments, parameters=schema)
    schema["properties"]["value"]["type"] = "string"

    assert tool.parameters["properties"]["value"]["type"] == "integer"  # type: ignore[index]
    with pytest.raises(TypeError):
        tool.parameters["properties"]["value"] = {}  # type: ignore[index]
    with pytest.raises(ValueError, match="valid JSON Schema"):
        _tool(
            lambda arguments: arguments,
            parameters={"type": "object", "properties": 7},
        )


@pytest.mark.asyncio
async def test_tool_executor_validates_arguments_before_handler() -> None:
    calls: list[dict[str, Any]] = []
    executor = ToolExecutor((_tool(lambda arguments: calls.append(arguments)),))

    with pytest.raises(AiToolError):
        await executor.execute("lookup", {"value": "1"})
    with pytest.raises(AiToolError):
        await executor.execute("lookup", {"value": 1, "extra": True})
    with pytest.raises(AiToolError):
        await executor.execute("missing", {"value": 1})
    assert calls == []


@pytest.mark.asyncio
async def test_tool_executor_runs_sync_handler_off_loop_and_serializes_strict_json() -> (
    None
):
    loop_thread = threading.get_ident()
    handler_threads: list[int] = []

    def handler(arguments: dict[str, Any]) -> dict[str, int]:
        handler_threads.append(threading.get_ident())
        return {"value": arguments["value"]}

    executor = ToolExecutor((_tool(handler),))
    result = await executor.execute_json("lookup", {"value": 3})

    assert result == '{"value":3}'
    assert handler_threads != [loop_thread]

    invalid = ToolExecutor((_tool(lambda arguments: {"value": math.nan}),))
    with pytest.raises(AiToolError):
        await invalid.execute("lookup", {"value": 1})


@pytest.mark.asyncio
async def test_tool_executor_enforces_timeout_and_result_size() -> None:
    async def slow(arguments: dict[str, Any]) -> dict[str, bool]:
        await asyncio.sleep(60)
        return {"ok": True}

    timeout = ToolExecutor((_tool(slow),), timeout_seconds=0.01)
    with pytest.raises(AiTimeoutError):
        await timeout.execute("lookup", {"value": 1})

    oversized = ToolExecutor((_tool(lambda arguments: "x" * 20),), max_result_bytes=10)
    with pytest.raises(AiToolError):
        await oversized.execute("lookup", {"value": 1})


@pytest.mark.asyncio
async def test_tool_batch_reserves_budget_before_any_handler_runs() -> None:
    calls: list[int] = []
    tool = _tool(lambda arguments: calls.append(arguments["value"]))
    executor = ToolExecutor((tool,), max_calls=2)
    await executor.execute("lookup", {"value": 1})

    with pytest.raises(AiToolError, match="budget"):
        await executor.execute_batch(
            (("lookup", {"value": 2}), ("lookup", {"value": 3}))
        )
    assert calls == [1]


def test_tool_executor_rejects_duplicate_names_and_invalid_limits() -> None:
    tool = _tool(lambda arguments: arguments)
    with pytest.raises(ValueError, match="unique"):
        ToolExecutor((tool, tool))
    with pytest.raises(ValueError):
        ToolExecutor((tool,), max_calls=0)
    with pytest.raises(ValueError, match="callable"):
        _tool(None)
