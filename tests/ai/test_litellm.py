from __future__ import annotations

import os
import asyncio
from typing import Any

import pytest

from nexus_kernel.ai import (
    AgentToolSpec,
    AiProviderError,
    AiTimeoutError,
    ChatMessage,
    ChatRequest,
    LiteLLMClient,
)


@pytest.mark.asyncio
async def test_litellm_passes_credentials_explicitly_and_closes_public_clients() -> (
    None
):
    calls: list[dict[str, Any]] = []
    closed: list[bool] = []
    environment_before = dict(os.environ)

    async def completion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "model": "provider/model",
            "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
            },
        }

    async def close_clients() -> None:
        closed.append(True)

    client = LiteLLMClient(
        api_key="explicit-secret",  # security-test-fixture
        model="provider/model",
        base_url="https://models.example/v1",
        completion=completion,
        close_clients=close_clients,
    )
    await client.start()
    tool = AgentToolSpec(
        name="lookup",
        description="Look up a value.",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments: {"ok": True},
    )
    response = await client.chat(
        ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
        tools=(tool,),
    )
    await client.close()

    assert response.message.content == "answer"
    assert response.provider_id == "litellm"
    assert response.usage.total_tokens == 7
    assert calls[0]["api_key"] == "explicit-secret"
    assert calls[0]["base_url"] == "https://models.example/v1"
    assert calls[0]["stream"] is False
    assert calls[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert calls[0]["tools"][0]["function"]["name"] == "lookup"
    assert closed == [True]
    assert dict(os.environ) == environment_before


@pytest.mark.asyncio
async def test_litellm_normalizes_timeout_and_exception() -> None:
    async def slow(**kwargs: Any) -> Any:
        await asyncio.sleep(60)

    async def no_close() -> None:
        return None

    timeout_client = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
        timeout_seconds=0.01,
        completion=slow,
        close_clients=no_close,
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="private prompt")])
    with pytest.raises(AiTimeoutError):
        await timeout_client.chat(request)

    async def broken(**kwargs: Any) -> Any:
        raise RuntimeError("Authorization: Bearer must-not-leak")

    broken_client = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
        completion=broken,
        close_clients=no_close,
    )
    with pytest.raises(AiProviderError) as captured:
        await broken_client.chat(request)
    assert "must-not-leak" not in str(captured.value)
    assert "private prompt" not in str(captured.value)


@pytest.mark.asyncio
async def test_litellm_concurrent_close_is_single_and_failure_can_retry() -> None:
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def completion(**kwargs: Any) -> Any:
        raise AssertionError("completion is not used")

    async def close_clients() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    client = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
        completion=completion,
        close_clients=close_clients,
    )
    first = asyncio.create_task(client.close())
    second = asyncio.create_task(client.close())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    await second
    assert calls == 1

    retry_calls = 0

    async def flaky_close() -> None:
        nonlocal retry_calls
        retry_calls += 1
        if retry_calls == 1:
            raise RuntimeError("private close detail")

    retrying = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
        completion=completion,
        close_clients=flaky_close,
    )
    with pytest.raises(AiProviderError) as captured:
        await retrying.close()
    assert captured.value.__cause__ is None
    assert "private close detail" not in str(captured.value)
    await retrying.close()
    assert retry_calls == 2
