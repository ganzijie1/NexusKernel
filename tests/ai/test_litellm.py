from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from nexus_kernel.ai import (
    AgentIdentity,
    AgentToolSpec,
    AiProviderError,
    AiTimeoutError,
    ChatMessage,
    ChatRequest,
    LiteLLMClient,
)


_IDENTITY = AgentIdentity("trusted-user")


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
        handler=lambda arguments, identity: {"ok": True},
    )
    response = await client.chat(
        ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
        identity=_IDENTITY,
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
        await timeout_client.chat(request, identity=_IDENTITY)

    async def broken(**kwargs: Any) -> Any:
        raise RuntimeError("Authorization: Bearer must-not-leak")

    broken_client = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
        completion=broken,
        close_clients=no_close,
    )
    with pytest.raises(AiProviderError) as captured:
        await broken_client.chat(request, identity=_IDENTITY)
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


@pytest.mark.asyncio
async def test_litellm_constructor_defers_default_callables_to_async_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    closed: list[bool] = []

    async def completion(**kwargs: Any) -> Any:
        raise AssertionError("completion is not used")

    async def close_clients() -> None:
        closed.append(True)

    def load_callables() -> tuple[Any, Any]:
        events.append("load")
        return completion, close_clients

    async def offload(function: Any, *args: Any) -> Any:
        events.append("to_thread")
        return function(*args)

    monkeypatch.setattr(
        LiteLLMClient, "_load_default_callables", staticmethod(load_callables)
    )
    monkeypatch.setattr("nexus_kernel.ai.litellm_client.asyncio.to_thread", offload)

    client = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
    )
    assert events == []

    await client.start()

    assert events == ["to_thread", "load"]
    await client.close()
    assert closed == [True]


@pytest.mark.asyncio
async def test_litellm_request_deadline_includes_lazy_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()

    async def completion(**kwargs: Any) -> dict[str, Any]:
        return {
            "model": "provider/model",
            "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}],
            "usage": {},
        }

    async def close_clients() -> None:
        return None

    async def offload(function: Any, *args: Any) -> tuple[Any, Any]:
        start_entered.set()
        await release_start.wait()
        return completion, close_clients

    monkeypatch.setattr("nexus_kernel.ai.litellm_client.asyncio.to_thread", offload)
    client = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
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
async def test_litellm_default_start_requires_local_cost_map_without_mutating_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)

    async def offload(function: Any, *args: Any) -> Any:
        return function(*args)

    monkeypatch.setattr("nexus_kernel.ai.litellm_client.asyncio.to_thread", offload)
    client = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
    )

    with pytest.raises(AiProviderError, match="initialization failed"):
        await client.start()

    assert "LITELLM_LOCAL_MODEL_COST_MAP" not in os.environ
    await client.close()


@pytest.mark.asyncio
async def test_close_failure_after_cancelled_waiter_is_observed_and_retryable() -> None:
    close_calls = 0
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()
    loop_errors: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> Any:
        raise AssertionError("completion is not used")

    async def close_clients() -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            entered.set()
            try:
                await release.wait()
                raise RuntimeError("token=private-close-detail")
            finally:
                finished.set()

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: loop_errors.append(context))
    try:
        client = LiteLLMClient(
            api_key="secret",  # security-test-fixture
            model="provider/model",
            completion=completion,
            close_clients=close_clients,
        )
        waiter = asyncio.create_task(client.close())
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        release.set()
        await finished.wait()
        await asyncio.sleep(0)
        await client.close()

        assert close_calls == 2
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_litellm_close_drains_admitted_call_and_rejects_new_calls() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    close_calls = 0

    async def completion(**kwargs: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {
            "model": "provider/model",
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            "usage": {},
        }

    async def close_clients() -> None:
        nonlocal close_calls
        close_calls += 1

    client = LiteLLMClient(
        api_key="secret",  # security-test-fixture
        model="provider/model",
        timeout_seconds=1,
        completion=completion,
        close_clients=close_clients,
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    running = asyncio.create_task(client.chat(request, identity=_IDENTITY))
    await entered.wait()

    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    with pytest.raises(AiProviderError, match="closed"):
        await client.chat(request, identity=_IDENTITY)
    assert close_calls == 0

    release.set()
    response = await running
    await closing
    assert response.message.content == "ok"
    assert close_calls == 1
