from __future__ import annotations

import asyncio
import math
import threading
from typing import Any

import pytest

import nexus_kernel.ai.tool_executor as tool_executor_module
from nexus_kernel.ai import AgentIdentity, AgentToolSpec, AiTimeoutError, AiToolError
from nexus_kernel.ai.tool_executor import ToolExecutor


_IDENTITY = AgentIdentity("trusted-user")


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
    tool = _tool(lambda arguments, identity: arguments, parameters=schema)
    schema["properties"]["value"]["type"] = "string"

    assert tool.parameters["properties"]["value"]["type"] == "integer"  # type: ignore[index]
    with pytest.raises(TypeError):
        tool.parameters["properties"]["value"] = {}  # type: ignore[index]
    with pytest.raises(ValueError, match="valid JSON Schema"):
        _tool(
            lambda arguments, identity: arguments,
            parameters={"type": "object", "properties": 7},
        )


@pytest.mark.asyncio
async def test_tool_executor_validates_arguments_before_handler() -> None:
    calls: list[dict[str, Any]] = []
    executor = ToolExecutor(
        (_tool(lambda arguments, identity: calls.append(arguments)),),
        identity=_IDENTITY,
    )

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
    identities: list[AgentIdentity] = []

    def handler(arguments: dict[str, Any], identity: AgentIdentity) -> dict[str, int]:
        handler_threads.append(threading.get_ident())
        identities.append(identity)
        return {"value": arguments["value"]}

    executor = ToolExecutor((_tool(handler),), identity=_IDENTITY)
    result = await executor.execute_json("lookup", {"value": 3})

    assert result == '{"value":3}'
    assert handler_threads != [loop_thread]
    assert identities == [_IDENTITY]

    invalid = ToolExecutor(
        (_tool(lambda arguments, identity: {"value": math.nan}),),
        identity=_IDENTITY,
    )
    with pytest.raises(AiToolError):
        await invalid.execute("lookup", {"value": 1})


@pytest.mark.asyncio
async def test_tool_executor_enforces_timeout_and_result_size() -> None:
    async def slow(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, bool]:
        await asyncio.sleep(60)
        return {"ok": True}

    timeout = ToolExecutor((_tool(slow),), identity=_IDENTITY, timeout_seconds=0.01)
    with pytest.raises(AiTimeoutError):
        await timeout.execute("lookup", {"value": 1})

    async def large(arguments: dict[str, Any], identity: AgentIdentity) -> str:
        return "x" * 20

    oversized = ToolExecutor(
        (_tool(large),),
        identity=_IDENTITY,
        max_result_bytes=10,
    )
    with pytest.raises(AiToolError):
        await oversized.execute("lookup", {"value": 1})


@pytest.mark.asyncio
async def test_tool_batch_reserves_budget_before_any_handler_runs() -> None:
    calls: list[int] = []

    async def record(arguments: dict[str, Any], identity: AgentIdentity) -> None:
        calls.append(arguments["value"])

    tool = _tool(record)
    executor = ToolExecutor((tool,), identity=_IDENTITY, max_calls=2)
    await executor.execute("lookup", {"value": 1})

    with pytest.raises(AiToolError, match="budget"):
        await executor.execute_batch(
            (("lookup", {"value": 2}), ("lookup", {"value": 3}))
        )
    assert calls == [1]


def test_tool_executor_rejects_duplicate_names_and_invalid_limits() -> None:
    tool = _tool(lambda arguments, identity: arguments)
    with pytest.raises(ValueError, match="unique"):
        ToolExecutor((tool, tool), identity=_IDENTITY)
    with pytest.raises(ValueError):
        ToolExecutor((tool,), identity=_IDENTITY, max_calls=0)
    with pytest.raises(ValueError):
        ToolExecutor((tool,), identity=_IDENTITY, max_argument_bytes=0)
    with pytest.raises(ValueError, match="callable"):
        _tool(None)


@pytest.mark.asyncio
async def test_tool_executor_rejects_non_strict_and_oversized_arguments() -> None:
    calls: list[dict[str, Any]] = []
    permissive = _tool(
        lambda arguments, identity: calls.append(arguments) or {"ok": True},
        parameters={"type": "object"},
    )
    executor = ToolExecutor((permissive,), identity=_IDENTITY, max_argument_bytes=24)
    non_string_key: Any = {1: "value"}

    for arguments in (
        non_string_key,
        {"value": (1, 2)},
        {"value": math.inf},
        {"value": object()},
        {"value": "x" * 40},
    ):
        with pytest.raises(AiToolError):
            await executor.execute("lookup", arguments)

    nested: Any = None
    for _ in range(70):
        nested = [nested]
    with pytest.raises(AiToolError):
        await executor.execute("lookup", {"value": nested})
    assert calls == []


@pytest.mark.asyncio
async def test_tool_executor_rejects_non_strict_result_without_coercion() -> None:
    for result in ((1, 2), {1: "value"}, object()):
        executor = ToolExecutor(
            (_tool(lambda arguments, identity: result),),
            identity=_IDENTITY,
        )
        with pytest.raises(AiToolError, match="strict JSON"):
            await executor.execute("lookup", {"value": 1})


@pytest.mark.asyncio
async def test_tool_result_limit_counts_actual_utf8_bytes() -> None:
    result = "\u4f1a\u8bdd"
    accepted = ToolExecutor(
        (_tool(lambda arguments, identity: result),),
        identity=_IDENTITY,
        max_result_bytes=8,
    )
    rejected = ToolExecutor(
        (_tool(lambda arguments, identity: result),),
        identity=_IDENTITY,
        max_result_bytes=7,
    )

    assert await accepted.execute_json("lookup", {"value": 1}) == '"\u4f1a\u8bdd"'
    with pytest.raises(AiToolError, match="size"):
        await rejected.execute_json("lookup", {"value": 1})


@pytest.mark.asyncio
async def test_tool_arguments_cannot_override_trusted_identity() -> None:
    captured: list[tuple[dict[str, Any], AgentIdentity]] = []

    async def handler(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, bool]:
        captured.append((arguments, identity))
        return {"ok": True}

    tool = _tool(
        handler,
        parameters={
            "type": "object",
            "properties": {"principal_id": {"type": "string"}},
            "required": ["principal_id"],
            "additionalProperties": False,
        },
    )
    executor = ToolExecutor((tool,), identity=_IDENTITY)

    await executor.execute("lookup", {"principal_id": "forged-tenant"})

    assert captured == [
        ({"principal_id": "forged-tenant"}, AgentIdentity("trusted-user"))
    ]


@pytest.mark.asyncio
async def test_tool_deadline_includes_argument_schema_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def blocked_offload(function: Any, *args: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "nexus_kernel.ai.tool_executor.asyncio.to_thread", blocked_offload
    )
    executor = ToolExecutor(
        (_tool(lambda arguments, identity: {"ok": True}),),
        identity=_IDENTITY,
        timeout_seconds=0.01,
    )

    with pytest.raises(AiTimeoutError):
        await executor.execute("lookup", {"value": 1})
    assert entered.is_set()


@pytest.mark.asyncio
async def test_tool_deadline_expires_during_argument_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_calls = 0

    async def handler(arguments: dict[str, Any], identity: AgentIdentity) -> None:
        nonlocal handler_calls
        handler_calls += 1

    def expired_visit(
        traversal: tool_executor_module._JsonTraversal, depth: int
    ) -> None:
        del traversal, depth
        raise TimeoutError

    monkeypatch.setattr(tool_executor_module._JsonTraversal, "visit", expired_visit)
    executor = ToolExecutor((_tool(handler),), identity=_IDENTITY)

    with pytest.raises(AiTimeoutError):
        await executor.execute("lookup", {"value": 1})
    assert handler_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted_stage", ("normalization", "encoding"))
async def test_tool_result_stages_share_and_enforce_the_call_deadline(
    monkeypatch: pytest.MonkeyPatch,
    exhausted_stage: str,
) -> None:
    handler_calls = 0
    observed_deadlines: list[float] = []
    original_copy = tool_executor_module._copy_strict_json
    original_encode = tool_executor_module._BoundedJsonEncoder.encode

    async def handler(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, bool]:
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    def record_copy(
        value: Any,
        traversal: tool_executor_module._JsonTraversal,
        depth: int = 0,
    ) -> Any:
        observed_deadlines.append(traversal.deadline)
        if exhausted_stage == "normalization" and handler_calls:
            raise TimeoutError
        return original_copy(value, traversal, depth)

    def record_encode(
        encoder: tool_executor_module._BoundedJsonEncoder, value: Any
    ) -> str:
        observed_deadlines.append(encoder._deadline)
        if exhausted_stage == "encoding" and handler_calls:
            raise TimeoutError
        return original_encode(encoder, value)

    monkeypatch.setattr(tool_executor_module, "_copy_strict_json", record_copy)
    monkeypatch.setattr(
        tool_executor_module._BoundedJsonEncoder, "encode", record_encode
    )
    executor = ToolExecutor((_tool(handler),), identity=_IDENTITY)

    with pytest.raises(AiTimeoutError):
        await executor.execute("lookup", {"value": 1})
    assert handler_calls == 1
    assert len(set(observed_deadlines)) == 1
