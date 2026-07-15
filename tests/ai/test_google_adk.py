from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexuskernel.ai import (
    AdkResult,
    AgentToolSpec,
    AiProviderError,
    AiTimeoutError,
    ChatMessage,
    ChatRequest,
    GoogleAdkRuntime,
)


class FakeAdkBackend:
    def __init__(self, result: AdkResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def run(self, **kwargs: Any) -> AdkResult:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_adk_maps_instruction_history_identity_and_ttl_session() -> None:
    backend = FakeAdkBackend(
        AdkResult(
            text="agent answer",
            model="gemini-test",
            input_tokens=5,
            output_tokens=2,
            total_tokens=7,
            finish_reason="stop",
        )
    )
    now = [100.0]
    runtime = GoogleAdkRuntime(
        api_key="explicit-secret",
        model="gemini-test",
        session_ttl_seconds=10,
        backend=backend,
        time_source=lambda: now[0],
    )
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content="system instruction"),
            ChatMessage(role="user", content="old question"),
            ChatMessage(role="assistant", content="old answer"),
            ChatMessage(role="user", content="new question"),
        ]
    )
    tool = AgentToolSpec(
        name="lookup",
        description="Look up a value.",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments: {"ok": True},
    )

    first = await runtime.run(
        request, user_id="user-1", session_id="session-1", tools=(tool,)
    )
    now[0] = 105.0
    await runtime.run(request, user_id="user-1", session_id="session-1")
    now[0] = 120.0
    await runtime.run(request, user_id="user-1", session_id="session-1")

    assert first.message.content == "agent answer"
    assert first.provider_id == "google_adk"
    assert first.usage.total_tokens == 7
    assert backend.calls[0]["system_instruction"] == "system instruction"
    assert [item.role for item in backend.calls[0]["history"]] == [
        "user",
        "assistant",
    ]
    assert backend.calls[0]["new_message"].content == "new question"
    assert backend.calls[0]["user_id"] == "user-1"
    assert backend.calls[0]["session_id"] == "session-1"
    assert backend.calls[0]["tools"][0].name == "lookup"
    assert [item["reset_session"] for item in backend.calls] == [True, False, True]
    assert not hasattr(runtime, "chat")

    await runtime.close()
    assert backend.closed is True


@pytest.mark.asyncio
async def test_adk_normalizes_timeout_and_errors() -> None:
    class SlowBackend(FakeAdkBackend):
        async def run(self, **kwargs: Any) -> AdkResult:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    request = ChatRequest(messages=[ChatMessage(role="user", content="private prompt")])
    runtime = GoogleAdkRuntime(
        api_key="secret",
        model="gemini-test",
        timeout_seconds=0.01,
        backend=SlowBackend(RuntimeError()),
    )
    with pytest.raises(AiTimeoutError):
        await runtime.run(request, user_id="user", session_id="session")

    broken = GoogleAdkRuntime(
        api_key="secret",
        model="gemini-test",
        backend=FakeAdkBackend(RuntimeError("token=must-not-leak")),
    )
    with pytest.raises(AiProviderError) as captured:
        await broken.run(request, user_id="user", session_id="session")
    assert "must-not-leak" not in str(captured.value)
    assert "private prompt" not in str(captured.value)
