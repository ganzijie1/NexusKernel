from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_kernel.ai import (
    AgentIdentity,
    AgentToolSpec,
    AiProviderError,
    AiTimeoutError,
    AiToolError,
    ChatMessage,
    ChatRequest,
    OpenAICompatibleClient,
)


_IDENTITY = AgentIdentity("trusted-user")


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
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


@pytest.mark.parametrize("field", ["timeout_seconds", "tool_timeout_seconds"])
@pytest.mark.parametrize(
    "value", [True, 0.0, -1.0, float("nan"), float("inf"), float("-inf")]
)
def test_openai_rejects_non_positive_finite_timeouts(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        OpenAICompatibleClient(
            api_key="test-secret",  # security-test-fixture
            model="fake-model",
            client=FakeOpenAI([]),
            **{field: value},
        )


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

    async def lookup_weather(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, str]:
        assert identity == _IDENTITY
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
        api_key="test-secret",  # security-test-fixture
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
        identity=_IDENTITY,
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
        api_key="secret",  # security-test-fixture
        model="fake",
        timeout_seconds=0.01,
        client=FakeOpenAI([slow]),
    )
    with pytest.raises(AiTimeoutError):
        await timeout_client.chat(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=_IDENTITY,
        )

    broken_client = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        client=FakeOpenAI([RuntimeError("api_key=must-not-leak")]),
    )
    with pytest.raises(AiProviderError) as captured:
        await broken_client.chat(
            ChatRequest(messages=[ChatMessage(role="user", content="private")]),
            identity=_IDENTITY,
        )
    assert "must-not-leak" not in str(captured.value)
    assert "private" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_openai_rejects_invalid_usage_and_recovers_health() -> None:
    fake = FakeOpenAI(
        [
            {
                **_response("bad"),
                "usage": {
                    "prompt_tokens": "2",
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
            _response("good"),
        ]
    )
    client = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        client=fake,  # security-test-fixture
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])

    with pytest.raises(AiProviderError) as captured:
        await client.chat(request, identity=_IDENTITY)
    assert captured.value.__cause__ is None
    assert await client.health_check() is False

    response = await client.chat(request, identity=_IDENTITY)
    assert response.message.content == "good"
    assert await client.health_check() is True


@pytest.mark.asyncio
async def test_openai_rejects_whitespace_only_response() -> None:
    client = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        client=FakeOpenAI([_response("   ")]),  # security-test-fixture
    )

    with pytest.raises(AiProviderError, match="no text"):
        await client.chat(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=_IDENTITY,
        )


@pytest.mark.asyncio
async def test_openai_tool_schema_budget_round_timeout_and_size_limits() -> None:
    handler_calls: list[int] = []

    async def handler(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, int]:
        assert identity == _IDENTITY
        handler_calls.append(arguments["value"])
        return {"value": arguments["value"]}

    tool = AgentToolSpec(
        name="lookup",
        description="Look up a value.",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=handler,
    )
    invalid_args = FakeOpenAI(
        [
            _response(
                None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "function": {"name": "lookup", "arguments": '{"value":"1"}'},
                    }
                ],
            )
        ]
    )
    client = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        client=invalid_args,  # security-test-fixture
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    with pytest.raises(AiToolError):
        await client.chat(request, identity=_IDENTITY, tools=(tool,))
    assert handler_calls == []

    for raw_arguments in (
        '{"value":1,"value":2}',
        '{"value":NaN}',
        '{"value":1' + (" " * 100) + "}",
    ):
        strict_call = _response(
            None,
            tool_calls=[
                {
                    "id": "call-strict",
                    "function": {"name": "lookup", "arguments": raw_arguments},
                }
            ],
        )
        strict_client = OpenAICompatibleClient(
            api_key="secret",  # security-test-fixture
            model="fake",
            max_tool_argument_bytes=32,
            client=FakeOpenAI([strict_call]),
        )
        with pytest.raises(AiToolError):
            await strict_client.chat(request, identity=_IDENTITY, tools=(tool,))
    assert handler_calls == []

    batch = FakeOpenAI(
        [
            _response(
                None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "function": {"name": "lookup", "arguments": '{"value":1}'},
                    },
                    {
                        "id": "call-2",
                        "function": {"name": "lookup", "arguments": '{"value":2}'},
                    },
                ],
            )
        ]
    )
    limited = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        max_tool_calls=1,
        client=batch,
    )
    with pytest.raises(AiToolError, match="budget"):
        await limited.chat(request, identity=_IDENTITY, tools=(tool,))
    assert handler_calls == []

    rounds = FakeOpenAI(
        [
            _response(
                None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "function": {"name": "lookup", "arguments": '{"value":1}'},
                    }
                ],
            ),
            _response(
                None,
                tool_calls=[
                    {
                        "id": "call-2",
                        "function": {"name": "lookup", "arguments": '{"value":2}'},
                    }
                ],
            ),
        ]
    )
    one_round = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        max_tool_rounds=1,
        client=rounds,
    )
    with pytest.raises(AiProviderError, match="round"):
        await one_round.chat(request, identity=_IDENTITY, tools=(tool,))
    assert handler_calls == [1]

    async def slow(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, bool]:
        await asyncio.sleep(60)
        return {"ok": True}

    slow_tool = AgentToolSpec(
        name="slow",
        description="Wait.",
        parameters={"type": "object", "properties": {}},
        handler=slow,
    )
    tool_call = _response(
        None,
        tool_calls=[{"id": "call", "function": {"name": "slow", "arguments": "{}"}}],
    )
    timeout = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        tool_timeout_seconds=0.01,
        client=FakeOpenAI([tool_call]),
    )
    with pytest.raises(AiTimeoutError):
        await timeout.chat(request, identity=_IDENTITY, tools=(slow_tool,))
    assert await timeout.health_check() is True

    async def large(arguments: dict[str, Any], identity: AgentIdentity) -> str:
        return "x" * 100

    large_tool = AgentToolSpec(
        name="large",
        description="Return a large result.",
        parameters={"type": "object", "properties": {}},
        handler=large,
    )
    large_call = _response(
        None,
        tool_calls=[{"id": "call", "function": {"name": "large", "arguments": "{}"}}],
    )
    oversized = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        max_tool_result_bytes=16,
        client=FakeOpenAI([large_call]),
    )
    with pytest.raises(AiToolError, match="size"):
        await oversized.chat(request, identity=_IDENTITY, tools=(large_tool,))


@pytest.mark.asyncio
async def test_openai_close_is_concurrent_idempotent() -> None:
    fake = FakeOpenAI([])
    client = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="fake",
        client=fake,  # security-test-fixture
    )

    await asyncio.gather(client.close(), client.close(), client.close())

    assert fake.close_calls == 1
    assert await client.health_check() is False


@pytest.mark.asyncio
async def test_openai_constructor_defers_default_client_to_async_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fake = FakeOpenAI([])

    def create_client(client: OpenAICompatibleClient) -> FakeOpenAI:
        events.append("create")
        return fake

    async def offload(function: Any, *args: Any) -> Any:
        events.append("to_thread")
        return function(*args)

    monkeypatch.setattr(OpenAICompatibleClient, "_create_default_client", create_client)
    monkeypatch.setattr("nexus_kernel.ai.openai_compatible.asyncio.to_thread", offload)

    client = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="model",
    )
    assert events == []

    await client.start()

    assert events == ["to_thread", "create"]
    await client.close()


@pytest.mark.asyncio
async def test_openai_request_deadline_includes_lazy_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    fake = FakeOpenAI([_response("answer")])

    async def offload(function: Any, *args: Any) -> FakeOpenAI:
        start_entered.set()
        await release_start.wait()
        return fake

    monkeypatch.setattr("nexus_kernel.ai.openai_compatible.asyncio.to_thread", offload)
    client = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=0.01,
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])

    with pytest.raises(AiTimeoutError):
        await client.chat(request, identity=_IDENTITY)
    assert start_entered.is_set()
    assert await client.health_check() is False

    release_start.set()
    start_task = client._start_task
    assert start_task is not None
    await asyncio.wait_for(start_task, timeout=0.2)
    response = await client.chat(request, identity=_IDENTITY)

    assert response.message.content == "answer"
    assert await client.health_check() is True
    await client.close()


@pytest.mark.asyncio
async def test_openai_close_drains_admitted_call_and_rejects_new_calls() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_response() -> dict[str, Any]:
        entered.set()
        await release.wait()
        return _response("answer")

    fake = FakeOpenAI([slow_response])
    client = OpenAICompatibleClient(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=1,
        client=fake,
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    running = asyncio.create_task(client.chat(request, identity=_IDENTITY))
    await entered.wait()

    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    with pytest.raises(AiProviderError, match="closed"):
        await client.chat(request, identity=_IDENTITY)
    assert fake.close_calls == 0

    release.set()
    response = await running
    await closing
    assert response.message.content == "answer"
    assert fake.close_calls == 1
