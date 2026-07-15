from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from nexuskernel.ai import (
    AgentToolSpec,
    AiProviderError,
    AiTimeoutError,
    ChatMessage,
    ChatRequest,
    OpenAICompatibleClient,
)


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return await response()
        return response


class FakeOpenAI:
    def __init__(self, responses: list[Any]) -> None:
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _response(
    content: str | None,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 2,
    completion_tokens: int = 3,
) -> dict[str, Any]:
    return {
        "model": "fake-model",
        "choices": [
            {
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {"content": content, "tool_calls": tool_calls or []},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@pytest.mark.asyncio
async def test_openai_maps_messages_tools_executes_calls_and_sums_usage() -> None:
    fake = FakeOpenAI(
        [
            _response(
                None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup_weather",
                            "arguments": '{"city":"Shanghai"}',
                        },
                    }
                ],
                prompt_tokens=4,
                completion_tokens=1,
            ),
            _response("sunny", prompt_tokens=6, completion_tokens=2),
        ]
    )
    tool_calls: list[str] = []

    async def lookup_weather(arguments: dict[str, Any]) -> dict[str, str]:
        tool_calls.append(arguments["city"])
        return {"weather": "sunny"}

    tool = AgentToolSpec(
        name="lookup_weather",
        description="Look up weather.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        handler=lookup_weather,
    )
    client = OpenAICompatibleClient(
        api_key="test-secret",
        model="fake-model",
        base_url="https://models.example/v1",
        timeout_seconds=1,
        client=fake,
    )
    await client.start()

    response = await client.chat(
        ChatRequest(
            messages=[
                ChatMessage(role="system", content="be concise"),
                ChatMessage(role="user", content="weather?"),
            ]
        ),
        tools=(tool,),
    )

    assert response.message.content == "sunny"
    assert response.provider_id == "openai_compatible"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens == 13
    assert tool_calls == ["Shanghai"]
    assert fake.completions.calls[0]["stream"] is False
    assert fake.completions.calls[0]["messages"][0] == {
        "role": "system",
        "content": "be concise",
    }
    assert fake.completions.calls[0]["tools"][0]["function"]["name"] == (
        "lookup_weather"
    )
    assert fake.completions.calls[1]["messages"][-1]["role"] == "tool"

    await client.close()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_openai_normalizes_timeout_and_provider_error_without_secrets() -> None:
    async def slow() -> Any:
        await asyncio.sleep(60)

    timeout_client = OpenAICompatibleClient(
        api_key="secret",
        model="fake",
        timeout_seconds=0.01,
        client=FakeOpenAI([slow]),
    )
    with pytest.raises(AiTimeoutError):
        await timeout_client.chat(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")])
        )

    broken_client = OpenAICompatibleClient(
        api_key="secret",
        model="fake",
        client=FakeOpenAI([RuntimeError("api_key=must-not-leak")]),
    )
    with pytest.raises(AiProviderError) as captured:
        await broken_client.chat(
            ChatRequest(messages=[ChatMessage(role="user", content="private")])
        )
    assert "must-not-leak" not in str(captured.value)
    assert "private" not in str(captured.value)
