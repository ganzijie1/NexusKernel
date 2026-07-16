from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from nexus_kernel.ai import (
    AdkResult,
    AgentIdentity,
    AgentToolSpec,
    AiProviderError,
    AiTimeoutError,
    BoundedMemoryAdkSessionStore,
    ChatMessage,
    ChatRequest,
    GoogleAdkRuntime,
    SessionKey,
)
from nexus_kernel.ai.google_adk import AdkSessionLease


class FakeAdkBackend:
    def __init__(self, result: AdkResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.deleted: list[SessionKey] = []
        self.close_calls = 0

    async def run(self, **kwargs: Any) -> AdkResult:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def delete_session(self, key: SessionKey) -> None:
        self.deleted.append(key)

    async def close(self) -> None:
        self.close_calls += 1


def _result() -> AdkResult:
    return AdkResult(
        text="agent answer",
        model="gemini-test",
        input_tokens=5,
        output_tokens=2,
        total_tokens=7,
        finish_reason="stop",
        event_count=3,
    )


def _request() -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role="system", content="system instruction"),
            ChatMessage(role="user", content="old question"),
            ChatMessage(role="assistant", content="old answer"),
            ChatMessage(role="user", content="new question"),
        ]
    )


@pytest.mark.asyncio
async def test_adk_maps_messages_trusted_identity_ttl_and_llm_limit() -> None:
    backend = FakeAdkBackend(_result())
    now = [100.0]
    runtime = GoogleAdkRuntime(
        api_key="explicit-secret",  # security-test-fixture
        model="gemini-test",
        session_ttl_seconds=10,
        max_tool_rounds=4,
        backend=backend,
        time_source=lambda: now[0],
    )
    tool = AgentToolSpec(
        name="lookup",
        description="Look up a value.",
        parameters={"type": "object", "properties": {}},
        handler=lambda arguments: {"ok": True},
    )
    identity = AgentIdentity("trusted-user")

    first = await runtime.run(
        _request(), identity=identity, session_id="session-1", tools=(tool,)
    )
    now[0] = 105.0
    await runtime.run(_request(), identity=identity, session_id="session-1")
    now[0] = 120.0
    await runtime.run(_request(), identity=identity, session_id="session-1")

    assert first.message.content == "agent answer"
    assert first.provider_id == "google_adk"
    assert first.usage.total_tokens == 7
    assert backend.calls[0]["system_instruction"] == "system instruction"
    assert [item.role for item in backend.calls[0]["history"]] == [
        "user",
        "assistant",
    ]
    assert backend.calls[0]["new_message"].content == "new question"
    assert backend.calls[0]["user_id"] == "trusted-user"
    assert backend.calls[0]["session_id"] == "session-1"
    assert backend.calls[0]["tool_executor"].tools[0].name == "lookup"
    assert backend.calls[0]["max_llm_calls"] == 5
    assert [item["reset_session"] for item in backend.calls] == [True, False, True]
    assert not hasattr(runtime, "chat")

    await runtime.close()
    await runtime.close()
    assert backend.close_calls == 1


@pytest.mark.asyncio
async def test_adk_temporary_session_is_deleted_and_not_request_id_derived() -> None:
    backend = FakeAdkBackend(_result())
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="gemini-test",
        backend=backend,  # security-test-fixture
    )

    await runtime.run(_request(), identity=AgentIdentity("service"))

    generated = backend.calls[0]["session_id"]
    assert isinstance(generated, str) and len(generated) == 32
    assert backend.deleted == [SessionKey("service", generated)]


@pytest.mark.asyncio
async def test_adk_timeout_and_backend_errors_discard_session_without_details() -> None:
    class SlowBackend(FakeAdkBackend):
        async def run(self, **kwargs: Any) -> AdkResult:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    request = ChatRequest(messages=[ChatMessage(role="user", content="private prompt")])
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="gemini-test",
        timeout_seconds=0.01,
        backend=SlowBackend(RuntimeError()),
    )
    with pytest.raises(AiTimeoutError):
        await runtime.run(request, identity=AgentIdentity("user"), session_id="session")
    assert await runtime.health_check() is False
    assert runtime._backend.deleted == [SessionKey("user", "session")]  # type: ignore[attr-defined]

    backend = FakeAdkBackend(RuntimeError("token=must-not-leak"))
    broken = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="gemini-test",
        backend=backend,  # security-test-fixture
    )
    with pytest.raises(AiProviderError) as captured:
        await broken.run(request, identity=AgentIdentity("user"), session_id="session")
    assert captured.value.__cause__ is None
    assert "must-not-leak" not in str(captured.value)
    assert "private prompt" not in str(captured.value)
    assert backend.deleted == [SessionKey("user", "session")]


@pytest.mark.asyncio
async def test_adk_health_recovers_after_valid_backend_result() -> None:
    invalid = AdkResult(text="answer", model="model", input_tokens=-1)
    backend = FakeAdkBackend(invalid)
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,  # security-test-fixture
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])

    with pytest.raises(AiProviderError):
        await runtime.run(request, identity=AgentIdentity("user"), session_id="session")
    assert await runtime.health_check() is False

    backend.result = _result()
    await runtime.run(request, identity=AgentIdentity("user"), session_id="session")
    assert await runtime.health_check() is True


@pytest.mark.asyncio
async def test_adk_store_exit_failure_discards_partial_backend_session() -> None:
    class ExitFailureStore:
        def __init__(self) -> None:
            self.invalidated: list[SessionKey] = []

        @asynccontextmanager
        async def lease(self, key: SessionKey, *, temporary: bool = False):
            yield AdkSessionLease(
                key,
                reset_session=True,
                evicted_keys=(),
                remaining_events=200,
            )
            raise RuntimeError("store-exit-private-detail")

        async def invalidate(self, key: SessionKey) -> None:
            self.invalidated.append(key)

        async def close(self) -> None:
            return None

    key = SessionKey("user", "session")
    backend = FakeAdkBackend(_result())
    store = ExitFailureStore()
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,  # security-test-fixture
        session_store=store,
    )

    with pytest.raises(AiProviderError) as captured:
        await runtime.run(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=AgentIdentity(key.principal_id),
            session_id=key.session_id,
        )

    assert captured.value.__cause__ is None
    assert backend.deleted == [key]
    assert store.invalidated == [key]


@pytest.mark.asyncio
async def test_adk_event_limit_deletes_session_before_next_turn() -> None:
    backend = FakeAdkBackend(AdkResult(text="answer", model="model", event_count=2))
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        session_max_events=2,
        backend=backend,
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    identity = AgentIdentity("user")

    await runtime.run(request, identity=identity, session_id="session")
    await runtime.run(request, identity=identity, session_id="session")

    assert backend.deleted == [
        SessionKey("user", "session"),
        SessionKey("user", "session"),
    ]
    assert [call["reset_session"] for call in backend.calls] == [True, True]


@pytest.mark.asyncio
async def test_adk_cancellation_propagates_after_session_cleanup() -> None:
    entered = asyncio.Event()

    class BlockingBackend(FakeAdkBackend):
        async def run(self, **kwargs: Any) -> AdkResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    backend = BlockingBackend(_result())
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    task = asyncio.create_task(
        runtime.run(
            request,
            identity=AgentIdentity("user"),
            session_id="session",
        )
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.deleted == [SessionKey("user", "session")]


@pytest.mark.asyncio
async def test_bounded_session_store_enforces_ttl_lru_and_event_limit() -> None:
    now = [0.0]
    store = BoundedMemoryAdkSessionStore(
        ttl_seconds=10,
        max_entries=2,
        max_events=2,
        time_source=lambda: now[0],
    )
    first = SessionKey("user", "first")
    second = SessionKey("user", "second")
    third = SessionKey("user", "third")

    async with store.lease(first) as lease:
        assert lease.reset_session is True
        lease.record_events(2)
    async with store.lease(first) as lease:
        assert lease.reset_session is True
    now[0] = 1.0
    async with store.lease(second):
        pass
    now[0] = 2.0
    async with store.lease(third) as lease:
        assert lease.evicted_keys == (first,)
    assert store.size == 2

    now[0] = 20.0
    async with store.lease(first) as lease:
        assert set(lease.evicted_keys) == {second, third}
    assert store.size == 1


@pytest.mark.asyncio
async def test_session_store_serializes_same_key_but_not_different_keys() -> None:
    store = BoundedMemoryAdkSessionStore(max_entries=3)
    first = SessionKey("user", "first")
    second = SessionKey("user", "second")
    same_entered = asyncio.Event()
    other_entered = asyncio.Event()

    async def take_same() -> None:
        async with store.lease(first):
            same_entered.set()

    async def take_other() -> None:
        async with store.lease(second):
            other_entered.set()

    async with store.lease(first):
        same_task = asyncio.create_task(take_same())
        other_task = asyncio.create_task(take_other())
        await asyncio.wait_for(other_entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert same_entered.is_set() is False
    await asyncio.gather(same_task, other_task)
    assert same_entered.is_set() is True


@pytest.mark.asyncio
async def test_adk_concurrent_close_survives_waiter_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowCloseBackend(FakeAdkBackend):
        async def close(self) -> None:
            self.close_calls += 1
            entered.set()
            await release.wait()

    backend = SlowCloseBackend(_result())
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,  # security-test-fixture
    )
    first = asyncio.create_task(runtime.close())
    second = asyncio.create_task(runtime.close())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    await second
    assert backend.close_calls == 1
