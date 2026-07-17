from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from nexus_kernel.ai import (
    AdkResult,
    AdkApplicationSpec,
    AdkSessionLease,
    AdkSessionStore,
    AgentIdentity,
    AgentToolSpec,
    AiProviderError,
    AiTimeoutError,
    AiWorkflowActionUnsupportedError,
    BoundedMemoryAdkSessionStore,
    ChatMessage,
    ChatRequest,
    GoogleAdkRuntime,
    JsonValue,
    SessionKey,
    TokenUsage,
    ToolExecutor,
    WorkflowInvocation,
    WorkflowInvocationResult,
    WorkflowNodeResult,
    WorkflowPendingAction,
    WorkflowActionResponse,
    WorkflowActionSpec,
    WorkflowRequestInputNodeSpec,
    WorkflowTransformNodeSpec,
)
from nexus_kernel.ai.google_adk import (
    _SdkAdkBackend,
    _accumulate_token_usage,
    _validate_adk_telemetry_environment,
)


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


class FakeWorkflowAdkBackend(FakeAdkBackend):
    def __init__(self, result: WorkflowInvocationResult) -> None:
        super().__init__(_result())
        self.workflow_result = result
        self.workflow_calls: list[dict[str, Any]] = []
        self.workflow_deleted: list[tuple[str, SessionKey]] = []

    async def invoke_workflow(self, **kwargs: Any) -> WorkflowInvocationResult:
        self.workflow_calls.append(kwargs)
        return self.workflow_result

    async def delete_workflow_session(self, key: SessionKey, *, app_name: str) -> None:
        self.workflow_deleted.append((app_name, key))


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
async def test_adk_workflow_runtime_isolates_application_sessions_and_cleanup() -> None:
    application = AdkApplicationSpec(
        agent_id="research_team",
        revision="v1",
        kind="team",
        factory=lambda context: object(),
    )
    backend = FakeWorkflowAdkBackend(
        WorkflowInvocationResult(
            state="succeeded",
            output="complete",
            usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
            model="model-v2",
            finish_reason="stop",
            event_count=4,
        )
    )
    now = [0.0]
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        max_tool_rounds=16,
        session_ttl_seconds=1,
        time_source=lambda: now[0],
        backend=backend,  # security-test-fixture
    )
    identity = AgentIdentity("tenant")

    result = await runtime.invoke(
        application,
        WorkflowInvocation(session_id="public-session", text="start"),
        identity=identity,
    )
    now[0] = 10.0
    continued = await runtime.invoke(
        application,
        WorkflowInvocation(session_id="public-session", text="continue"),
        identity=identity,
    )
    await runtime.delete_session(
        application,
        identity=identity,
        session_id="public-session",
    )

    assert result.output == "complete"
    assert continued.output == "complete"
    call = backend.workflow_calls[0]
    assert len(backend.workflow_calls) == 2
    assert call["application"] is application
    assert call["app_name"].startswith("nexus_kernel_research_team_")
    assert len(call["app_name"].rsplit("_", 1)[-1]) == 64
    assert call["user_id"] == identity.principal_id
    assert call["session_id"] != "public-session"
    assert call["session_id"].startswith("wf:")
    assert call["max_llm_calls"] == 17
    assert backend.workflow_deleted == [
        (call["app_name"], SessionKey(identity.principal_id, call["session_id"])),
        (call["app_name"], SessionKey(identity.principal_id, call["session_id"])),
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_adk_workflow_uses_phase_timeout_and_model_scoped_definition() -> None:
    class DelayedWorkflowBackend(FakeWorkflowAdkBackend):
        async def invoke_workflow(self, **kwargs: Any) -> WorkflowInvocationResult:
            await asyncio.sleep(0.03)
            return await super().invoke_workflow(**kwargs)

    application = AdkApplicationSpec(
        agent_id="scoped_workflow",
        revision="v1",
        kind="graph",
        factory=lambda context: object(),
    )
    result = WorkflowInvocationResult(state="succeeded", output="complete")
    first_backend = DelayedWorkflowBackend(result)
    first = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model-a",
        timeout_seconds=0.01,
        session_cleanup_timeout_seconds=0.005,
        backend=first_backend,  # security-test-fixture
    )
    second_backend = FakeWorkflowAdkBackend(result)
    second = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model-b",
        timeout_seconds=0.01,
        session_cleanup_timeout_seconds=0.005,
        backend=second_backend,  # security-test-fixture
    )
    invocation = WorkflowInvocation(session_id="session", text="start")
    identity = AgentIdentity("tenant")

    assert (
        await first.invoke(
            application,
            invocation,
            identity=identity,
            timeout_seconds=0.1,
        )
    ).output == "complete"
    await second.invoke(application, invocation, identity=identity)

    first_call = first_backend.workflow_calls[0]
    second_call = second_backend.workflow_calls[0]
    assert first_call["app_name"] != second_call["app_name"]
    assert first_call["session_id"] != second_call["session_id"]
    with pytest.raises(ValueError, match="positive and finite"):
        await first.invoke(
            application,
            invocation,
            identity=identity,
            timeout_seconds=float("inf"),
        )
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_adk_workflow_pins_actions_and_fails_closed_at_event_limit() -> None:
    action = WorkflowActionSpec(
        code="details",
        kind="input",
        response_schema={"type": "string"},
    )
    application = AdkApplicationSpec(
        agent_id="pinned_team",
        revision="v1",
        kind="team",
        factory=lambda context: object(),
        action_specs=(action,),
        resumable=True,
    )
    backend = FakeWorkflowAdkBackend(
        WorkflowInvocationResult(
            state="requires_action",
            event_count=1,
            actions=(WorkflowPendingAction("runtime-action", "details", "input"),),
            resume_token="invocation",  # security-test-fixture
        )
    )
    now = [0.0]
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        session_ttl_seconds=1,
        session_max_events=2,
        time_source=lambda: now[0],
        backend=backend,  # security-test-fixture
    )
    identity = AgentIdentity("tenant")

    pending = await runtime.invoke(
        application,
        WorkflowInvocation(session_id="session", text="start"),
        identity=identity,
    )
    now[0] = 10.0
    backend.workflow_result = WorkflowInvocationResult(
        state="succeeded",
        output="done",
        event_count=1,
    )
    with pytest.raises(AiProviderError, match="exceeds session limits"):
        await runtime.invoke(
            application,
            WorkflowInvocation(
                session_id="session",
                resume_token=pending.resume_token,
                responses=(WorkflowActionResponse("runtime-action", "input", "ready"),),
            ),
            identity=identity,
        )

    assert len(backend.workflow_calls) == 2
    assert len(backend.workflow_deleted) == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_adk_close_retires_pinned_workflow_session() -> None:
    application = AdkApplicationSpec(
        agent_id="paused_team",
        revision="v1",
        kind="team",
        factory=lambda context: object(),
        action_specs=(WorkflowActionSpec("details", "input", {"type": "string"}),),
        resumable=True,
    )
    backend = FakeWorkflowAdkBackend(
        WorkflowInvocationResult(
            state="requires_action",
            event_count=1,
            actions=(WorkflowPendingAction("runtime-action", "details", "input"),),
            resume_token="invocation",  # security-test-fixture
        )
    )
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,  # security-test-fixture
    )

    await runtime.invoke(
        application,
        WorkflowInvocation(session_id="session", text="start"),
        identity=AgentIdentity("tenant"),
    )
    await runtime.close()

    assert len(backend.workflow_deleted) == 2
    assert backend.close_calls == 1


@pytest.mark.asyncio
async def test_adk_maps_messages_trusted_identity_ttl_and_llm_limit() -> None:
    backend = FakeAdkBackend(_result())
    now = [100.0]
    runtime = GoogleAdkRuntime(
        api_key="explicit-secret",  # security-test-fixture
        model="gemini-test",
        session_ttl_seconds=10,
        max_tool_rounds=16,
        backend=backend,
        time_source=lambda: now[0],
    )
    tool_identities: list[AgentIdentity] = []

    async def lookup(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, bool]:
        tool_identities.append(identity)
        return {"ok": True}

    tool = AgentToolSpec(
        name="lookup",
        description="Look up a value.",
        parameters={"type": "object", "properties": {}},
        handler=lookup,
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
    assert await backend.calls[0]["tool_executor"].execute("lookup", {}) == {"ok": True}
    assert tool_identities == [identity]
    assert backend.calls[0]["max_llm_calls"] == 17
    assert backend.deleted == [
        SessionKey("trusted-user", "session-1"),
        SessionKey("trusted-user", "session-1"),
    ]
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
    assert backend.deleted == [
        SessionKey("service", generated),
        SessionKey("service", generated),
    ]


@pytest.mark.asyncio
async def test_adk_timeout_and_backend_errors_discard_session_without_details() -> None:
    class SlowBackend(FakeAdkBackend):
        async def run(self, **kwargs: Any) -> AdkResult:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    request = ChatRequest(messages=[ChatMessage(role="user", content="private prompt")])
    slow_backend = SlowBackend(RuntimeError())
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="gemini-test",
        timeout_seconds=0.01,
        session_cleanup_timeout_seconds=0.01,
        backend=slow_backend,
    )
    with pytest.raises(AiTimeoutError):
        await runtime.run(request, identity=AgentIdentity("user"), session_id="session")
    await asyncio.sleep(0)
    assert await runtime.health_check() is False
    assert slow_backend.deleted == [
        SessionKey("user", "session"),
        SessionKey("user", "session"),
    ]

    backend = FakeAdkBackend(RuntimeError("token=must-not-leak"))
    broken = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="gemini-test",
        backend=backend,  # security-test-fixture
    )
    with pytest.raises(AiProviderError) as captured:
        await broken.run(request, identity=AgentIdentity("user"), session_id="session")
    await asyncio.sleep(0)
    assert captured.value.__cause__ is None
    assert "must-not-leak" not in str(captured.value)
    assert "private prompt" not in str(captured.value)
    assert backend.deleted == [
        SessionKey("user", "session"),
        SessionKey("user", "session"),
    ]


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
            self.completed: list[SessionKey] = []

        @asynccontextmanager
        async def lease(
            self, key: SessionKey, *, temporary: bool = False
        ) -> AsyncIterator[AdkSessionLease]:
            lease = AdkSessionLease(
                key,
                reset_session=True,
                cleanup_keys=(),
                remaining_events=200,
            )
            try:
                yield lease
            finally:
                if lease.touched:
                    lease.mark_cleanup_required()
            raise RuntimeError("store-exit-private-detail")

        async def complete_cleanup(self, key: SessionKey) -> None:
            self.completed.append(key)

        async def pin(self, key: SessionKey) -> None:
            del key

        async def unpin(self, key: SessionKey) -> None:
            del key

        async def close(self) -> None:
            return None

    key = SessionKey("user", "session")
    backend = FakeAdkBackend(_result())
    external_store = ExitFailureStore()
    store: AdkSessionStore = external_store
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
    await asyncio.sleep(0)
    assert backend.deleted == [key, key]
    assert external_store.completed == [key, key]


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
        SessionKey("user", "session"),
        SessionKey("user", "session"),
    ]


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
    await asyncio.sleep(0)
    assert backend.deleted == [
        SessionKey("user", "session"),
        SessionKey("user", "session"),
    ]


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
        assert lease.cleanup_keys == (first,)
        cleanup_keys = lease.cleanup_keys
    for cleanup_key in cleanup_keys:
        await store.complete_cleanup(cleanup_key)
    assert store.size == 2

    now[0] = 20.0
    async with store.lease(first) as lease:
        assert set(lease.cleanup_keys) == {second, third}
        cleanup_keys = lease.cleanup_keys
    for cleanup_key in cleanup_keys:
        await store.complete_cleanup(cleanup_key)
    assert store.size == 1


@pytest.mark.asyncio
async def test_session_store_pin_prevents_ttl_reset_and_lru_eviction() -> None:
    now = [0.0]
    store = BoundedMemoryAdkSessionStore(
        ttl_seconds=1,
        max_entries=1,
        time_source=lambda: now[0],
    )
    first = SessionKey("user", "first")
    second = SessionKey("user", "second")

    async with store.lease(first) as lease:
        await lease.reserve_cleanup()
        lease.mark_touched()
        await store.pin(first)
    now[0] = 10.0
    async with store.lease(first) as lease:
        assert lease.reset_session is False

    entered = asyncio.Event()
    cleanup_keys: list[SessionKey] = []

    async def acquire_second() -> None:
        async with store.lease(second) as lease:
            cleanup_keys.extend(lease.cleanup_keys)
            entered.set()

    task = asyncio.create_task(acquire_second())
    await asyncio.sleep(0)
    assert entered.is_set() is False

    await store.unpin(first)
    await asyncio.wait_for(entered.wait(), timeout=1)
    await task
    assert cleanup_keys == [first]
    await store.complete_cleanup(first)


@pytest.mark.asyncio
async def test_session_finalizer_cancellation_tombstones_touched_session() -> None:
    finalizer_entered = asyncio.Event()

    class PausingStore(BoundedMemoryAdkSessionStore):
        async def _complete_lease(
            self,
            key: SessionKey,
            *,
            entry: Any,
            lease: AdkSessionLease | None,
            temporary: bool,
            acquired: bool,
        ) -> None:
            finalizer_entered.set()
            await super()._complete_lease(
                key,
                entry=entry,
                lease=lease,
                temporary=temporary,
                acquired=acquired,
            )

    store = PausingStore(max_entries=1)
    key = SessionKey("user", "session")
    touched = asyncio.Event()
    leave_body = asyncio.Event()

    async def use_session() -> None:
        async with store.lease(key) as lease:
            await lease.reserve_cleanup()
            lease.mark_touched()
            touched.set()
            await leave_body.wait()

    task = asyncio.create_task(use_session())
    await touched.wait()
    async with store._condition:
        leave_body.set()
        await finalizer_entered.wait()
        task.cancel()
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.size == 0
    assert store.cleanup_size == 1
    await store.complete_cleanup(key)


@pytest.mark.asyncio
async def test_session_store_waits_until_all_expired_entries_fit_tombstones() -> None:
    now = [0.0]
    store = BoundedMemoryAdkSessionStore(
        ttl_seconds=1,
        max_entries=3,
        time_source=lambda: now[0],
    )
    keys = [SessionKey("user", name) for name in "abcdef"]
    for key in keys[:3]:
        async with store.lease(key):
            pass
    async with store.lease(keys[3]) as lease:
        assert lease.cleanup_keys == (keys[0],)
    async with store.lease(keys[4]) as lease:
        assert lease.cleanup_keys == (keys[1],)

    now[0] = 2.0
    acquired = asyncio.Event()
    cleanup_keys: list[SessionKey] = []

    async def acquire_after_backlog() -> None:
        async with store.lease(keys[5]) as lease:
            cleanup_keys.extend(lease.cleanup_keys)
            acquired.set()

    task = asyncio.create_task(acquire_after_backlog())
    await asyncio.sleep(0)
    assert acquired.is_set() is False

    await store.complete_cleanup(keys[0])
    await asyncio.sleep(0)
    assert acquired.is_set() is False

    await store.complete_cleanup(keys[1])
    await asyncio.wait_for(acquired.wait(), timeout=1)
    await task
    assert set(cleanup_keys) == set(keys[2:5])
    for key in cleanup_keys:
        await store.complete_cleanup(key)


@pytest.mark.asyncio
async def test_session_store_waits_when_entry_and_tombstone_capacity_are_full() -> None:
    store = BoundedMemoryAdkSessionStore(max_entries=1)
    first = SessionKey("user", "first")
    second = SessionKey("user", "second")
    third = SessionKey("user", "third")

    async with store.lease(first):
        pass
    async with store.lease(second) as lease:
        assert lease.cleanup_keys == (first,)

    third_entered = asyncio.Event()
    third_cleanup_keys: list[SessionKey] = []

    async def acquire_third() -> None:
        async with store.lease(third) as lease:
            third_cleanup_keys.extend(lease.cleanup_keys)
            third_entered.set()

    third_task = asyncio.create_task(acquire_third())
    await asyncio.sleep(0)

    assert third_entered.is_set() is False
    assert store.size == 1
    assert store.cleanup_size == 1

    await store.complete_cleanup(first)
    await asyncio.wait_for(third_entered.wait(), timeout=1)
    await third_task

    assert third_cleanup_keys == [second]
    assert store.size == 1
    assert store.cleanup_size == 1
    await store.complete_cleanup(second)


@pytest.mark.asyncio
async def test_session_store_reserves_tombstone_capacity_before_backend_touch() -> None:
    store = BoundedMemoryAdkSessionStore(max_entries=1)
    first = SessionKey("user", "first")
    second = SessionKey("user", "second")

    async with store.lease(first):
        pass
    async with store.lease(second) as lease:
        assert lease.cleanup_keys == (first,)

    entered = asyncio.Event()

    async def retire_second() -> None:
        async with store.lease(second) as lease:
            entered.set()
            await lease.reserve_cleanup()
            lease.mark_touched()

    retiring = asyncio.create_task(retire_second())
    await entered.wait()
    retiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(retiring, timeout=0.2)
    assert store.size == 1
    assert store.cleanup_size == 1

    await store.complete_cleanup(first)
    async with store.lease(second) as lease:
        assert lease.reset_session is True
        await lease.reserve_cleanup()
        lease.mark_touched()
    assert store.size == 1
    assert store.cleanup_size == 0
    await store.close()


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
async def test_failed_session_tombstone_blocks_same_key_until_cleanup_finishes() -> (
    None
):
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    class CleanupBarrierBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(_result())
            self.delete_attempts = 0

        async def run(self, **kwargs: Any) -> AdkResult:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("private partial state")
            return _result()

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                self.deleted.append(key)
                return
            cleanup_entered.set()
            await release_cleanup.wait()
            self.deleted.append(key)

    backend = CleanupBarrierBackend()
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,  # security-test-fixture
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    identity = AgentIdentity("user")

    with pytest.raises(AiProviderError):
        await runtime.run(request, identity=identity, session_id="session")
    await asyncio.wait_for(cleanup_entered.wait(), timeout=1)

    retry = asyncio.create_task(
        runtime.run(request, identity=identity, session_id="session")
    )
    await asyncio.sleep(0)
    assert len(backend.calls) == 1

    release_cleanup.set()
    response = await asyncio.wait_for(retry, timeout=1)

    assert response.message.content == "agent answer"
    assert len(backend.calls) == 2
    assert backend.deleted == [
        SessionKey("user", "session"),
        SessionKey("user", "session"),
        SessionKey("user", "session"),
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_delete_keeps_tombstone_and_same_key_retries_cleanup() -> None:
    class FailingDeleteBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(_result())
            self.delete_attempts = 0

        async def run(self, **kwargs: Any) -> AdkResult:
            self.calls.append(kwargs)
            return AdkResult(
                text="agent answer",
                model="model",
                event_count=1 if len(self.calls) == 1 else 0,
            )

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 2:
                raise RuntimeError("token=private-delete-detail")
            self.deleted.append(key)

    key = SessionKey("user", "session")
    backend = FailingDeleteBackend()
    store = BoundedMemoryAdkSessionStore(max_events=1)
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,  # security-test-fixture
        session_store=store,
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    identity = AgentIdentity(key.principal_id)

    with pytest.raises(AiProviderError) as captured:
        await runtime.run(request, identity=identity, session_id=key.session_id)

    assert captured.value.__cause__ is None
    assert "private-delete-detail" not in str(captured.value)
    assert store.cleanup_size == 1

    response = await runtime.run(
        request,
        identity=identity,
        session_id=key.session_id,
    )

    assert response.message.content == "agent answer"
    assert backend.delete_attempts == 4
    assert backend.deleted == [key, key, key]
    assert store.cleanup_size == 0
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ignore_cancellation", [False, True])
async def test_cleanup_timeout_keeps_tracking_cooperative_and_noncooperative_delete(
    ignore_cancellation: bool,
) -> None:
    cleanup_entered = asyncio.Event()
    cleanup_finished = asyncio.Event()
    release_cleanup = asyncio.Event()

    class TimeoutDeleteBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(_result())
            self.cleanup_cancellations = 0
            self.delete_attempts = 0

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                self.deleted.append(key)
                return
            cleanup_entered.set()
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    self.cleanup_cancellations += 1
                    if not ignore_cancellation:
                        raise
            self.deleted.append(key)
            cleanup_finished.set()

    key = SessionKey("user", "session")
    backend = TimeoutDeleteBackend()
    store = BoundedMemoryAdkSessionStore(max_events=1)
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=1,
        session_cleanup_timeout_seconds=0.01,
        backend=backend,  # security-test-fixture
        session_store=store,
    )

    with pytest.raises(AiTimeoutError, match="cleanup timed out"):
        await runtime.run(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=AgentIdentity(key.principal_id),
            session_id=key.session_id,
        )

    await asyncio.wait_for(cleanup_entered.wait(), timeout=0.2)
    await asyncio.sleep(0)
    assert store.cleanup_size == 1
    assert backend.cleanup_cancellations == 1
    assert await runtime.health_check() is False

    with pytest.raises(AiProviderError, match="close failed"):
        await runtime.close()
    assert backend.close_calls == 0
    assert store.cleanup_size == 1

    release_cleanup.set()
    await runtime.close()
    assert cleanup_finished.is_set()
    assert store.cleanup_size == 0
    assert backend.deleted == [key, key]
    assert backend.close_calls == 1


@pytest.mark.asyncio
async def test_cleanup_budget_cancels_after_request_deadline_has_expired() -> None:
    cleanup_cancelled = asyncio.Event()

    class DeadlineBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(AdkResult(text="answer", model="model", event_count=1))
            self.delete_attempts = 0

        async def run(self, **kwargs: Any) -> AdkResult:
            await asyncio.sleep(0.012)
            return await super().run(**kwargs)

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts != 2:
                self.deleted.append(key)
                return
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_cancelled.set()
                raise

    key = SessionKey("user", "session")
    backend = DeadlineBackend()
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=0.02,
        session_cleanup_timeout_seconds=0.015,
        session_max_events=1,
        backend=backend,  # security-test-fixture
    )

    with pytest.raises(AiTimeoutError):
        await runtime.run(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=AgentIdentity(key.principal_id),
            session_id=key.session_id,
        )

    await asyncio.wait_for(cleanup_cancelled.wait(), timeout=0.2)
    assert backend.delete_attempts == 2
    assert await runtime.health_check() is False

    await runtime.close()
    assert backend.delete_attempts == 3


@pytest.mark.asyncio
async def test_close_sends_second_cancel_to_noncooperative_cleanup() -> None:
    release_cleanup = asyncio.Event()
    second_cancel = asyncio.Event()

    class RepeatedCancelBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(AdkResult(text="answer", model="model", event_count=1))
            self.delete_attempts = 0
            self.cleanup_cancellations = 0

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                self.deleted.append(key)
                return
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    self.cleanup_cancellations += 1
                    if self.cleanup_cancellations >= 2:
                        second_cancel.set()
            self.deleted.append(key)

    key = SessionKey("user", "session")
    backend = RepeatedCancelBackend()
    store = BoundedMemoryAdkSessionStore(max_events=1)
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=1,
        session_cleanup_timeout_seconds=0.01,
        backend=backend,  # security-test-fixture
        session_store=store,
    )

    with pytest.raises(AiTimeoutError):
        await runtime.run(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=AgentIdentity(key.principal_id),
            session_id=key.session_id,
        )
    assert backend.cleanup_cancellations == 1

    with pytest.raises(AiProviderError, match="close failed"):
        await runtime.close()
    await asyncio.wait_for(second_cancel.wait(), timeout=0.2)
    assert backend.close_calls == 0
    assert store.cleanup_size == 1

    release_cleanup.set()
    await runtime.close()
    assert backend.close_calls == 1
    assert store.cleanup_size == 0


@pytest.mark.asyncio
async def test_ttl_reset_uses_tracked_cleanup_budget_and_recovers_health() -> None:
    now = [0.0]
    release_cleanup = asyncio.Event()

    class SlowTtlCleanupBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(AdkResult(text="answer", model="model", event_count=0))
            self.delete_attempts = 0
            self.cleanup_cancellations = 0

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 2:
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    self.cleanup_cancellations += 1
                    raise
            self.deleted.append(key)

    key = SessionKey("user", "session")
    backend = SlowTtlCleanupBackend()
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=1,
        session_cleanup_timeout_seconds=0.01,
        session_ttl_seconds=1,
        backend=backend,  # security-test-fixture
        time_source=lambda: now[0],
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    identity = AgentIdentity(key.principal_id)

    await runtime.run(request, identity=identity, session_id=key.session_id)
    now[0] = 2.0
    with pytest.raises(AiTimeoutError, match="cleanup timed out"):
        await runtime.run(request, identity=identity, session_id=key.session_id)

    await asyncio.sleep(0)
    assert backend.cleanup_cancellations == 1
    assert len(backend.calls) == 1
    assert await runtime.health_check() is False

    release_cleanup.set()
    response = await runtime.run(request, identity=identity, session_id=key.session_id)

    assert response.message.content == "answer"
    assert backend.delete_attempts == 4
    assert backend.deleted == [key, key, key]
    assert await runtime.health_check() is True
    await runtime.close()


@pytest.mark.asyncio
async def test_unresolved_cleanup_keeps_other_key_success_degraded_until_retry() -> (
    None
):
    class CrossKeyBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(AdkResult(text="answer", model="model", event_count=0))
            self.delete_attempts: dict[SessionKey, int] = {}
            self.fail_a_cleanup = True

        async def run(self, **kwargs: Any) -> AdkResult:
            self.calls.append(kwargs)
            event_count = 1 if kwargs["session_id"] == "a" else 0
            return AdkResult(text="answer", model="model", event_count=event_count)

        async def delete_session(self, key: SessionKey) -> None:
            attempt = self.delete_attempts.get(key, 0) + 1
            self.delete_attempts[key] = attempt
            if key.session_id == "a" and attempt == 2 and self.fail_a_cleanup:
                raise RuntimeError("private cleanup detail")
            self.deleted.append(key)

    backend = CrossKeyBackend()
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        session_max_events=1,
        backend=backend,  # security-test-fixture
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    identity = AgentIdentity("user")

    with pytest.raises(AiProviderError):
        await runtime.run(request, identity=identity, session_id="a")
    response = await runtime.run(request, identity=identity, session_id="b")

    assert response.message.content == "answer"
    assert await runtime.health_check() is False

    backend.fail_a_cleanup = False
    recovered = await runtime.run(request, identity=identity, session_id="a")

    assert recovered.message.content == "answer"
    assert await runtime.health_check() is True
    await runtime.close()


@pytest.mark.asyncio
async def test_backend_failure_is_not_cleared_by_detached_cleanup_success() -> None:
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    class FailedBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(RuntimeError("private backend detail"))
            self.delete_attempts = 0

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                self.deleted.append(key)
                return
            cleanup_entered.set()
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    pass
            self.deleted.append(key)

    backend = FailedBackend()
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        session_cleanup_timeout_seconds=0.01,
        backend=backend,  # security-test-fixture
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])

    with pytest.raises(AiProviderError):
        await runtime.run(request, identity=AgentIdentity("user"), session_id="a")
    await asyncio.wait_for(cleanup_entered.wait(), timeout=0.2)

    release_cleanup.set()
    async with asyncio.timeout(0.2):
        while runtime._has_unresolved_cleanup():
            await asyncio.sleep(0)

    assert await runtime.health_check() is False
    await runtime.close()


@pytest.mark.asyncio
async def test_immediate_same_key_retry_replaces_just_cancelled_cleanup() -> None:
    class ImmediateRetryBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(AdkResult(text="answer", model="model", event_count=1))
            self.delete_attempts = 0

        async def run(self, **kwargs: Any) -> AdkResult:
            self.calls.append(kwargs)
            return AdkResult(
                text="answer",
                model="model",
                event_count=1 if len(self.calls) == 1 else 0,
            )

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts == 2:
                await asyncio.Event().wait()
            self.deleted.append(key)

    backend = ImmediateRetryBackend()
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=1,
        session_cleanup_timeout_seconds=0.01,
        session_max_events=1,
        backend=backend,  # security-test-fixture
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    identity = AgentIdentity("user")

    with pytest.raises(AiTimeoutError):
        await runtime.run(request, identity=identity, session_id="session")
    response = await runtime.run(request, identity=identity, session_id="session")

    assert response.message.content == "answer"
    assert await runtime.health_check() is True
    await runtime.close()


@pytest.mark.asyncio
async def test_close_retries_failed_cleanup_before_closing_backend_and_store() -> None:
    class RecordingStore(BoundedMemoryAdkSessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    class CloseRetryBackend(FakeAdkBackend):
        def __init__(self) -> None:
            super().__init__(RuntimeError("token=private-run-detail"))
            self.delete_attempts = 0

        async def delete_session(self, key: SessionKey) -> None:
            self.delete_attempts += 1
            if self.delete_attempts in {2, 3}:
                raise RuntimeError("token=private-delete-detail")
            self.deleted.append(key)

    key = SessionKey("user", "session")
    backend = CloseRetryBackend()
    store = RecordingStore()
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,  # security-test-fixture
        session_store=store,
    )

    with pytest.raises(AiProviderError):
        await runtime.run(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=AgentIdentity(key.principal_id),
            session_id=key.session_id,
        )
    async with asyncio.timeout(0.2):
        while backend.delete_attempts < 1:
            await asyncio.sleep(0)

    with pytest.raises(AiProviderError) as captured:
        await runtime.close()

    assert captured.value.__cause__ is None
    assert "private-delete-detail" not in str(captured.value)
    assert backend.delete_attempts == 3
    assert backend.close_calls == 0
    assert store.close_calls == 0
    assert store.cleanup_size == 1

    await runtime.close()

    assert backend.delete_attempts == 4
    assert backend.close_calls == 1
    assert store.close_calls == 1
    assert store.cleanup_size == 0


@pytest.mark.asyncio
async def test_close_does_not_close_backend_while_request_is_active() -> None:
    run_entered = asyncio.Event()
    release_run = asyncio.Event()

    class ActiveRunBackend(FakeAdkBackend):
        async def run(self, **kwargs: Any) -> AdkResult:
            self.calls.append(kwargs)
            run_entered.set()
            await release_run.wait()
            return AdkResult(text="answer", model="model", event_count=0)

    backend = ActiveRunBackend(_result())
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=1,
        session_cleanup_timeout_seconds=0.01,
        backend=backend,  # security-test-fixture
    )
    running = asyncio.create_task(
        runtime.run(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=AgentIdentity("user"),
            session_id="session",
        )
    )
    await run_entered.wait()

    with pytest.raises(AiProviderError, match="close failed"):
        await runtime.close()
    assert backend.close_calls == 0
    with pytest.raises(AiProviderError, match="closed"):
        await runtime.run(
            ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
            identity=AgentIdentity("user"),
            session_id="other",
        )

    release_run.set()
    response = await running
    assert response.message.content == "answer"

    await runtime.close()
    assert backend.close_calls == 1


@pytest.mark.asyncio
async def test_request_timeout_does_not_wait_for_blocking_session_cleanup() -> None:
    cleanup_entered = asyncio.Event()
    cleanup_finished = asyncio.Event()
    release_cleanup = asyncio.Event()

    class BlockingCleanupBackend(FakeAdkBackend):
        async def run(self, **kwargs: Any) -> AdkResult:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def delete_session(self, key: SessionKey) -> None:
            cleanup_entered.set()
            await release_cleanup.wait()
            self.deleted.append(key)
            cleanup_finished.set()

    backend = BlockingCleanupBackend(_result())
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=0.01,
        session_cleanup_timeout_seconds=0.01,
        backend=backend,  # security-test-fixture
    )

    with pytest.raises(AiTimeoutError):
        await asyncio.wait_for(
            runtime.run(
                ChatRequest(messages=[ChatMessage(role="user", content="hello")]),
                identity=AgentIdentity("user"),
                session_id="session",
            ),
            timeout=0.2,
        )

    await asyncio.wait_for(cleanup_entered.wait(), timeout=0.2)
    release_cleanup.set()
    await runtime.close()
    assert cleanup_finished.is_set()
    assert backend.deleted == [SessionKey("user", "session")]


def test_adk_usage_is_accumulated_across_multiple_events() -> None:
    first = SimpleNamespace(
        prompt_token_count=3,  # security-test-fixture
        tool_use_prompt_token_count=2,  # security-test-fixture
        candidates_token_count=2,  # security-test-fixture
        thoughts_token_count=1,  # security-test-fixture
        cached_content_token_count=100,  # security-test-fixture
        total_token_count=8,  # security-test-fixture
    )
    second = SimpleNamespace(
        prompt_token_count=4,  # security-test-fixture
        tool_use_prompt_token_count=1,  # security-test-fixture
        candidates_token_count=1,  # security-test-fixture
        thoughts_token_count=3,  # security-test-fixture
        cached_content_token_count=200,  # security-test-fixture
        total_token_count=9,  # security-test-fixture
    )

    usage = _accumulate_token_usage((0, 0, 0), first)
    usage = _accumulate_token_usage(usage, second)

    assert usage == (10, 7, 17)


def test_adk_rejects_inconsistent_sdk_total_usage() -> None:
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=FakeAdkBackend(_result()),  # security-test-fixture
    )

    with pytest.raises(AiProviderError, match="inconsistent token usage"):
        runtime._normalize_result(
            AdkResult(
                text="answer",
                model="model",
                input_tokens=3,
                output_tokens=2,
                total_tokens=4,
            )
        )
    with pytest.raises(AiProviderError, match="inconsistent token usage"):
        _SdkAdkBackend._validated_total_usage((3, 2, 4))


@pytest.mark.parametrize("captures_content", [False, True])
def test_adk_admin_telemetry_lock_requires_safe_environment(
    monkeypatch: pytest.MonkeyPatch, captures_content: bool
) -> None:
    telemetry = ModuleType("google.adk.telemetry")

    class TelemetryConfig:
        should_add_content_to_logs = captures_content
        should_add_content_to_experimental_spans = False
        should_add_content_to_legacy_spans = False

    telemetry.TelemetryConfig = TelemetryConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.adk.telemetry", telemetry)
    monkeypatch.setenv("ADK_TELEMETRY_IGNORE_RUN_CONFIG", "true")

    if captures_content:
        with pytest.raises(AiProviderError, match="content capture is unsafe"):
            _validate_adk_telemetry_environment()
    else:
        _validate_adk_telemetry_environment()


def test_adk_cleanup_timeout_must_not_exceed_request_timeout() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        GoogleAdkRuntime(
            api_key="secret",  # security-test-fixture
            model="model",
            timeout_seconds=1,
            session_cleanup_timeout_seconds=1.1,
            backend=FakeAdkBackend(_result()),  # security-test-fixture
        )


@pytest.mark.parametrize("concurrency", [0, 5, True])
def test_adk_workflow_concurrency_is_bounded(concurrency: object) -> None:
    with pytest.raises(ValueError, match="between 1 and 4"):
        GoogleAdkRuntime(
            api_key="secret",  # security-test-fixture
            model="model",
            max_workflow_concurrency=concurrency,  # type: ignore[arg-type]
            backend=FakeAdkBackend(_result()),  # security-test-fixture
        )


@pytest.mark.asyncio
async def test_adk_constructor_defers_default_backend_to_async_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = FakeAdkBackend(_result())

    def create_backend(runtime: GoogleAdkRuntime) -> FakeAdkBackend:
        events.append("create")
        return backend

    async def offload(function: Any, *args: Any) -> Any:
        events.append("to_thread")
        return function(*args)

    monkeypatch.setattr(GoogleAdkRuntime, "_create_default_backend", create_backend)
    monkeypatch.setattr("nexus_kernel.ai.google_adk.asyncio.to_thread", offload)

    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
    )
    assert events == []

    await runtime.start()

    assert events == ["to_thread", "create"]
    await runtime.close()


@pytest.mark.asyncio
async def test_adk_request_deadline_includes_lazy_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    backend = FakeAdkBackend(AdkResult(text="answer", model="model", event_count=0))

    async def offload(function: Any, *args: Any) -> FakeAdkBackend:
        start_entered.set()
        await release_start.wait()
        return backend

    monkeypatch.setattr("nexus_kernel.ai.google_adk.asyncio.to_thread", offload)
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=0.01,
        session_cleanup_timeout_seconds=0.005,
    )
    request = ChatRequest(messages=[ChatMessage(role="user", content="hello")])

    with pytest.raises(AiTimeoutError):
        await runtime.run(request, identity=AgentIdentity("user"), session_id="session")
    assert start_entered.is_set()
    assert await runtime.health_check() is False

    release_start.set()
    start_task = runtime._start_task
    assert start_task is not None
    await asyncio.wait_for(start_task, timeout=0.2)
    response = await runtime.run(
        request, identity=AgentIdentity("user"), session_id="session"
    )

    assert response.message.content == "answer"
    assert await runtime.health_check() is True
    await runtime.close()


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


@pytest.mark.asyncio
async def test_close_disposes_delayed_start_without_cancelling_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    backend = FakeAdkBackend(_result())

    class RecordingStore(BoundedMemoryAdkSessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    store = RecordingStore()

    async def offload(function: Any, *args: Any) -> FakeAdkBackend:
        start_entered.set()
        await release_start.wait()
        return backend

    monkeypatch.setattr("nexus_kernel.ai.google_adk.asyncio.to_thread", offload)
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=1,
        session_cleanup_timeout_seconds=0.01,
        session_store=store,
    )
    starting = asyncio.create_task(runtime.start())
    await start_entered.wait()

    with pytest.raises(AiProviderError, match="close failed"):
        await runtime.close()
    start_task = runtime._start_task
    assert start_task is not None
    assert start_task.cancelled() is False

    with pytest.raises(AiProviderError, match="close failed"):
        await runtime.close()
    assert runtime._start_task is start_task
    assert start_task.cancelled() is False

    release_start.set()
    with pytest.raises(AiProviderError, match="closed"):
        await starting
    async with asyncio.timeout(0.2):
        while backend.close_calls == 0 or store.close_calls == 0:
            await asyncio.sleep(0)

    await runtime.close()
    assert backend.close_calls == 1
    assert store.close_calls == 1


@pytest.mark.asyncio
async def test_close_closes_store_after_delayed_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()

    class RecordingStore(BoundedMemoryAdkSessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    store = RecordingStore()

    async def offload(function: Any, *args: Any) -> Any:
        start_entered.set()
        await release_start.wait()
        raise RuntimeError("private start detail")

    monkeypatch.setattr("nexus_kernel.ai.google_adk.asyncio.to_thread", offload)
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        timeout_seconds=1,
        session_cleanup_timeout_seconds=0.01,
        session_store=store,
    )
    starting = asyncio.create_task(runtime.start())
    await start_entered.wait()

    with pytest.raises(AiProviderError, match="close failed"):
        await runtime.close()
    release_start.set()
    with pytest.raises(AiProviderError, match="initialization failed"):
        await starting
    async with asyncio.timeout(0.2):
        while store.close_calls == 0:
            await asyncio.sleep(0)

    await runtime.close()
    assert store.close_calls == 1


@pytest.mark.asyncio
async def test_sdk_backend_withholds_client_close_for_unsettled_runner() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class StubbornRunner:
        async def close(self) -> None:
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass

    class FakeAioClient:
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    aio = FakeAioClient()
    backend = object.__new__(_SdkAdkBackend)
    backend._client = SimpleNamespace(aio=aio)
    backend._runner_cleanups = {}
    backend._runner_cleanup_timeout_seconds = 0.01
    cleanup = backend._schedule_runner_cleanup(StubbornRunner())
    await entered.wait()

    with pytest.raises(AiTimeoutError):
        await cleanup
    assert backend.has_unresolved_runner_cleanup is True
    with pytest.raises(AiProviderError, match="incomplete"):
        await backend.close()
    assert aio.close_calls == 0

    release.set()
    async with asyncio.timeout(0.2):
        while any(
            not record.close_task.done() for record in backend._runner_cleanups.values()
        ):
            await asyncio.sleep(0)
    assert backend.has_unresolved_runner_cleanup is False
    await backend.close()
    assert aio.close_calls == 1


@pytest.mark.asyncio
async def test_sdk_backend_fake_covers_sessions_tools_usage_and_runner_close(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = ModuleType("google")
    google.__path__ = []  # type: ignore[attr-defined]
    adk = ModuleType("google.adk")
    adk.__path__ = []  # type: ignore[attr-defined]
    agents = ModuleType("google.adk.agents")
    agents.__path__ = []  # type: ignore[attr-defined]
    apps = ModuleType("google.adk.apps")
    run_config = ModuleType("google.adk.agents.run_config")
    events_module = ModuleType("google.adk.events")
    runners = ModuleType("google.adk.runners")
    telemetry = ModuleType("google.adk.telemetry")
    tools_module = ModuleType("google.adk.tools")
    tools_module.__path__ = []  # type: ignore[attr-defined]
    base_tool = ModuleType("google.adk.tools.base_tool")
    genai = ModuleType("google.genai")

    class Part:
        def __init__(self, *, text: str, thought: bool = False) -> None:
            self.text = text
            self.thought = thought

    class Content:
        def __init__(self, *, role: str, parts: list[Part]) -> None:
            self.role = role
            self.parts = parts

    class FunctionDeclaration:
        @classmethod
        def model_validate(cls, value: dict[str, Any]) -> dict[str, Any]:
            return value

    class Event:
        def __init__(self, *, author: str, content: Content) -> None:
            self.author = author
            self.content = content

    class LlmAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.name = kwargs["name"]
            self.tools = kwargs["tools"]

    class App:
        def __init__(self, *, name: str, root_agent: LlmAgent) -> None:
            self.name = name
            self.root_agent = root_agent

    class ContentCapturingMode:
        NO_CONTENT = object()

    class TelemetryConfig:
        def __init__(self, *, capture_message_content: object) -> None:
            self.capture_message_content = capture_message_content

    class RunConfig:
        def __init__(self, *, max_llm_calls: int, telemetry: TelemetryConfig) -> None:
            self.max_llm_calls = max_llm_calls
            self.telemetry = telemetry

    class BaseTool:
        def __init__(self, *, name: str, description: str) -> None:
            self.name = name
            self.description = description

    runner_close_calls: list[bool] = []
    tool_results: list[Any] = []

    class Runner:
        def __init__(self, **kwargs: Any) -> None:
            assert set(kwargs) == {"app", "session_service"}
            self.agent = kwargs["app"].root_agent
            self.sessions = kwargs["session_service"]

        def run_async(self, **kwargs: Any) -> AsyncIterator[Any]:
            assert kwargs["run_config"].max_llm_calls == 5
            assert (
                kwargs["run_config"].telemetry.capture_message_content
                is ContentCapturingMode.NO_CONTENT
            )

            async def generate() -> AsyncIterator[Any]:
                session = await self.sessions.get_session()
                session.events.append(SimpleNamespace(author="user"))
                tool_results.append(
                    await self.agent.tools[0].run_async(
                        args={"value": 7}, tool_context=None
                    )
                )
                retry_error = SimpleNamespace(
                    author="agent",
                    error_code="retryable",
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=2,  # security-test-fixture
                        tool_use_prompt_token_count=1,  # security-test-fixture
                        candidates_token_count=1,  # security-test-fixture
                        thoughts_token_count=2,  # security-test-fixture
                        cached_content_token_count=100,  # security-test-fixture
                        total_token_count=6,  # security-test-fixture
                    ),
                    model_version="retry-model",
                    finish_reason="retry",
                    is_final_response=lambda: False,
                    content=None,
                )
                session.events.append(retry_error)
                yield retry_error
                child_final = SimpleNamespace(
                    author="child",
                    error_code=None,
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=3,  # security-test-fixture
                        tool_use_prompt_token_count=2,  # security-test-fixture
                        candidates_token_count=2,  # security-test-fixture
                        thoughts_token_count=1,  # security-test-fixture
                        cached_content_token_count=200,  # security-test-fixture
                        total_token_count=8,  # security-test-fixture
                    ),
                    model_version="child-model",
                    finish_reason="child-stop",
                    is_final_response=lambda: True,
                    content=Content(role="model", parts=[Part(text="child answer")]),
                )
                session.events.append(child_final)
                yield child_final
                root_final = SimpleNamespace(
                    author="agent",
                    error_code=None,
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=4,  # security-test-fixture
                        tool_use_prompt_token_count=3,  # security-test-fixture
                        candidates_token_count=3,  # security-test-fixture
                        thoughts_token_count=2,  # security-test-fixture
                        cached_content_token_count=300,  # security-test-fixture
                        total_token_count=12,  # security-test-fixture
                    ),
                    model_version="model-v2",
                    finish_reason="stop",
                    is_final_response=lambda: True,
                    content=Content(
                        role="model",
                        parts=[
                            Part(text="private reasoning", thought=True),
                            Part(text="answer"),
                        ],
                    ),
                )
                session.events.append(root_final)
                yield root_final

            return generate()

        async def close(self) -> None:
            runner_close_calls.append(True)

    agents.LlmAgent = LlmAgent  # type: ignore[attr-defined]
    apps.App = App  # type: ignore[attr-defined]
    run_config.RunConfig = RunConfig  # type: ignore[attr-defined]
    events_module.Event = Event  # type: ignore[attr-defined]
    runners.Runner = Runner  # type: ignore[attr-defined]
    telemetry.ContentCapturingMode = ContentCapturingMode  # type: ignore[attr-defined]
    telemetry.TelemetryConfig = TelemetryConfig  # type: ignore[attr-defined]
    base_tool.BaseTool = BaseTool  # type: ignore[attr-defined]
    genai.types = SimpleNamespace(  # type: ignore[attr-defined]
        Content=Content,
        Part=Part,
        FunctionDeclaration=FunctionDeclaration,
    )
    modules = {
        "google": google,
        "google.adk": adk,
        "google.adk.agents": agents,
        "google.adk.apps": apps,
        "google.adk.agents.run_config": run_config,
        "google.adk.events": events_module,
        "google.adk.runners": runners,
        "google.adk.telemetry": telemetry,
        "google.adk.tools": tools_module,
        "google.adk.tools.base_tool": base_tool,
        "google.genai": genai,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    class FakeSession:
        def __init__(self) -> None:
            self.events: list[Any] = []

    class FakeSessions:
        def __init__(self) -> None:
            self.session: FakeSession | None = None
            self.appended: list[Event] = []
            self.delete_calls = 0

        async def get_session(self, **kwargs: Any) -> FakeSession | None:
            return self.session

        async def create_session(self, **kwargs: Any) -> FakeSession:
            self.session = FakeSession()
            return self.session

        async def append_event(self, session: FakeSession, event: Event) -> None:
            assert session is self.session
            self.appended.append(event)
            session.events.append(event)

        async def delete_session(self, **kwargs: Any) -> None:
            self.delete_calls += 1
            self.session = None

    class FakeAio:
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    sessions = FakeSessions()
    aio = FakeAio()
    backend = object.__new__(_SdkAdkBackend)
    backend._sessions = sessions
    backend._app_name = "app"
    backend._agent_name = "agent"
    backend._model = object()
    backend._model_name = "model"
    backend._client = SimpleNamespace(aio=aio)
    backend._runner_cleanups = {}
    backend._runner_cleanup_timeout_seconds = 0.1
    identities: list[AgentIdentity] = []

    async def handler(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, int]:
        identities.append(identity)
        return {"value": arguments["value"]}

    identity = AgentIdentity("tenant")
    executor = ToolExecutor(
        (
            AgentToolSpec(
                name="lookup",
                description="Look up a value.",
                parameters={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "value": {
                            "oneOf": [
                                {"const": 7},
                                {"type": "string"},
                            ]
                        }
                    },
                    "required": ["value"],
                },
                handler=handler,
            ),
        ),
        identity=identity,
    )

    result = await backend.run(
        system_instruction="system",
        history=(
            ChatMessage(role="user", content="old"),
            ChatMessage(role="assistant", content="answer"),
        ),
        new_message=ChatMessage(role="user", content="new"),
        user_id=identity.principal_id,
        session_id="session",
        tool_executor=executor,
        max_llm_calls=5,
    )
    declaration = executor.tools[0]
    sdk_declaration = backend._tool_adapter(declaration, executor)._get_declaration()
    await backend.delete_session(SessionKey(identity.principal_id, "session"))
    await backend.close()

    assert result.text == "answer"
    assert result.model == "model-v2"
    assert result.finish_reason == "stop"
    assert result.event_count == 6
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (
        15,
        11,
        26,
    )
    assert "parameters_json_schema" in sdk_declaration
    assert "parameters" not in sdk_declaration
    assert len(sessions.appended) == 2
    assert sessions.delete_calls == 1
    assert tool_results == [{"value": 7}]
    assert identities == [identity]
    assert runner_close_calls == [True]
    assert aio.close_calls == 1


@pytest.mark.asyncio
async def test_sdk_workflow_uses_public_adk_resume_and_root_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.events import Event
    from google.adk.runners import Runner as RealRunner
    from google.adk.telemetry import ContentCapturingMode
    from google.adk.workflow import START, JoinNode, Workflow
    from google.genai import types

    del RealRunner
    phases = ["initial", "resume"]
    runner_close_calls = 0

    class FakeRunner:
        def __init__(self, *, app: App, session_service: Any) -> None:
            self.app = app
            self.session_service = session_service
            assert app.resumability_config is not None
            assert app.resumability_config.is_resumable is True

        def run_async(self, **kwargs: Any) -> AsyncIterator[Event]:
            phase = phases.pop(0)
            assert kwargs["run_config"].max_llm_calls == 5
            assert (
                kwargs["run_config"].telemetry.capture_message_content
                is ContentCapturingMode.NO_CONTENT
            )

            async def generate() -> AsyncIterator[Event]:
                session = await self.session_service.get_session(
                    app_name=self.app.name,
                    user_id=kwargs["user_id"],
                    session_id=kwargs["session_id"],
                )
                assert session is not None
                invocation_id = kwargs["invocation_id"] or "invocation-1"
                await self.session_service.append_event(
                    session,
                    Event(
                        invocation_id=invocation_id,
                        author="user",
                        content=kwargs["new_message"],
                    ),
                )
                if phase == "initial":
                    assert kwargs["invocation_id"] is None
                    action = Event(
                        invocation_id=invocation_id,
                        author="research_flow",
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    function_call=types.FunctionCall(
                                        id="runtime-action-1",
                                        name="adk_request_input",
                                        args={
                                            "message": "provide_details",
                                            "response_schema": {
                                                "type": "object",
                                                "properties": {
                                                    "answer": {"type": "string"}
                                                },
                                                "required": ["answer"],
                                                "additionalProperties": False,
                                            },
                                        },
                                    )
                                ),
                                types.Part(
                                    function_call=types.FunctionCall(
                                        id="runtime-action-2",
                                        name="adk_request_input",
                                        args={
                                            "payload": {"code": "confirm_details"},
                                            "response_schema": {"type": "boolean"},
                                        },
                                    )
                                ),
                            ],
                        ),
                        long_running_tool_ids={
                            "runtime-action-1",
                            "runtime-action-2",
                        },
                        usage_metadata=types.GenerateContentResponseUsageMetadata(
                            prompt_token_count=2,  # security-test-fixture
                            tool_use_prompt_token_count=1,  # security-test-fixture
                            candidates_token_count=1,  # security-test-fixture
                            thoughts_token_count=1,  # security-test-fixture
                            total_token_count=5,  # security-test-fixture
                        ),
                    )
                    await self.session_service.append_event(session, action)
                    yield action
                    return

                assert kwargs["invocation_id"] == "invocation-1"
                responses = [
                    part.function_response for part in kwargs["new_message"].parts
                ]
                assert [response.id for response in responses] == [
                    "runtime-action-1",
                    "runtime-action-2",
                ]
                assert all(
                    response.name == "adk_request_input" for response in responses
                )
                assert responses[0].response == {"answer": "ready"}
                assert responses[1].response == {"result": True}
                output = Event(
                    invocation_id=invocation_id,
                    author="research_flow",
                    output={"complete": True},
                    node_path="research_flow@1/terminal@1",
                    model_version="model-v2",
                    finish_reason=types.FinishReason.STOP,
                    usage_metadata=types.GenerateContentResponseUsageMetadata(
                        prompt_token_count=3,  # security-test-fixture
                        candidates_token_count=2,  # security-test-fixture
                        total_token_count=5,  # security-test-fixture
                    ),
                )
                await self.session_service.append_event(session, output)
                yield output

            return generate()

        async def close(self) -> None:
            nonlocal runner_close_calls
            runner_close_calls += 1

    monkeypatch.setattr("google.adk.runners.Runner", FakeRunner)
    backend = _SdkAdkBackend(
        api_key="secret",  # security-test-fixture
        model="model",
    )
    factory_models: list[object] = []

    async def lookup(
        arguments: dict[str, Any], identity: AgentIdentity
    ) -> dict[str, bool]:
        del arguments, identity
        return {"ok": True}

    tool = AgentToolSpec(
        name="lookup",
        description="Look up data.",
        parameters={"type": "object", "properties": {}},
        handler=lookup,
    )

    def factory(context: Any) -> App:
        factory_models.append(context.model)
        worker = LlmAgent(
            name="researcher",
            model=context.model,
            mode="single_turn",
            tools=[context.tools["lookup"]],
        )
        terminal = JoinNode(name="terminal")
        return App(
            name="factory_app",
            root_agent=Workflow(
                name="research_flow",
                edges=[(START, worker, terminal)],
                max_concurrency=2,
            ),
        )

    action_spec = WorkflowActionSpec(
        code="provide_details",
        kind="input",
        response_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    confirmation_spec = WorkflowActionSpec(
        code="confirm_details",
        kind="confirmation",
        response_schema={"type": "boolean"},
    )
    application = AdkApplicationSpec(
        agent_id="research_flow",
        revision="v1",
        kind="graph",
        factory=factory,
        tool_names=("lookup",),
        action_specs=(action_spec, confirmation_spec),
        output_format="json",
        resumable=True,
    )
    runtime = GoogleAdkRuntime(
        api_key="secret",  # security-test-fixture
        model="model",
        backend=backend,
    )
    identity = AgentIdentity("tenant")

    pending = await runtime.invoke(
        application,
        WorkflowInvocation(session_id="session", text="begin"),
        identity=identity,
        tools=(tool,),
    )
    completed = await runtime.invoke(
        application,
        WorkflowInvocation(
            session_id="session",
            resume_token=pending.resume_token,
            responses=(
                WorkflowActionResponse(
                    runtime_action_id=pending.actions[0].runtime_action_id,
                    kind="input",
                    value={"answer": "ready"},
                ),
                WorkflowActionResponse(
                    runtime_action_id=pending.actions[1].runtime_action_id,
                    kind="confirmation",
                    value=True,
                ),
            ),
        ),
        identity=identity,
        tools=(tool,),
    )

    assert pending.state == "requires_action"
    assert pending.actions[0].code == "provide_details"
    assert pending.actions[1].code == "confirm_details"
    assert pending.resume_token == "invocation-1"
    assert pending.event_count == 2
    assert pending.usage == TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5)
    assert completed.state == "succeeded"
    assert completed.output == {"complete": True}
    assert completed.event_count == 2
    assert completed.model == "model-v2"
    assert completed.finish_reason == "STOP"
    assert factory_models == [backend._model, backend._model]
    assert runner_close_calls == 2
    await runtime.delete_session(application, identity=identity, session_id="session")
    await runtime.close()


@pytest.mark.asyncio
async def test_sdk_workflow_runs_managed_transform_sequence_and_route() -> None:
    from google.adk.apps import App
    from google.adk.workflow import Edge, START, Workflow

    observed: list[JsonValue] = []

    def classify(value: JsonValue) -> WorkflowNodeResult:
        observed.append(value)
        return WorkflowNodeResult(
            output={"decision": "accepted"},
            route="accepted",
        )

    def finish(value: JsonValue) -> WorkflowNodeResult:
        observed.append(value)
        assert isinstance(value, Mapping)
        return WorkflowNodeResult(output={"complete": value["decision"] == "accepted"})

    def factory(context: Any) -> App:
        classify_node = context.function_nodes["classify"]
        finish_node = context.function_nodes["finish"]
        return App(
            name="factory_app",
            root_agent=Workflow(
                name="managed_flow",
                edges=[
                    Edge(from_node=START, to_node=classify_node),
                    Edge(
                        from_node=classify_node,
                        to_node=finish_node,
                        route="accepted",
                    ),
                ],
                max_concurrency=1,
            ),
        )

    application = AdkApplicationSpec(
        agent_id="managed_flow",
        revision="v1",
        kind="graph",
        factory=factory,
        function_nodes=(
            WorkflowTransformNodeSpec(
                name="classify",
                transform=classify,
                input_schema={"type": "string"},
                output_schema={
                    "type": "object",
                    "properties": {"decision": {"const": "accepted"}},
                    "required": ["decision"],
                    "additionalProperties": False,
                },
                routes=("accepted",),
            ),
            WorkflowTransformNodeSpec(
                name="finish",
                transform=finish,
                input_schema={"type": "object"},
                output_schema={
                    "type": "object",
                    "properties": {"complete": {"type": "boolean"}},
                    "required": ["complete"],
                    "additionalProperties": False,
                },
            ),
        ),
        output_format="json",
    )
    backend = _SdkAdkBackend(
        api_key="secret",  # security-test-fixture
        model="model",
    )
    try:
        result = await backend.invoke_workflow(
            application=application,
            invocation=WorkflowInvocation(session_id="session", text="begin"),
            app_name="managed_app",
            user_id="tenant",
            session_id="session",
            tool_executor=ToolExecutor((), identity=AgentIdentity("tenant")),
            max_llm_calls=1,
        )
    finally:
        await backend.close()

    assert result.state == "succeeded"
    assert result.output == {"complete": True}
    assert observed[0] == "begin"
    assert isinstance(observed[1], Mapping)


@pytest.mark.asyncio
async def test_managed_sync_transform_does_not_block_or_complete_after_cancel() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_transform(value: JsonValue) -> WorkflowNodeResult:
        started.set()
        release.wait(timeout=1)
        return WorkflowNodeResult(output=value)

    application = AdkApplicationSpec(
        agent_id="responsive_transform",
        revision="v1",
        kind="graph",
        factory=lambda context: object(),
        function_nodes=(
            WorkflowTransformNodeSpec(
                name="slow_transform",
                transform=slow_transform,
            ),
        ),
    )
    backend = object.__new__(_SdkAdkBackend)
    nodes, _ = backend._build_workflow_function_nodes(application)
    transform = nodes["slow_transform"]._func
    task = asyncio.create_task(transform("input"))
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        before = time.monotonic()
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        assert time.monotonic() - before < 0.1
        assert task.done() is False

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)
        release.set()
        await asyncio.sleep(0.01)
        assert task.cancelled() is True
    finally:
        release.set()
        timer.cancel()


@pytest.mark.asyncio
async def test_sdk_workflow_resumes_managed_request_input_node() -> None:
    from google.adk.apps import App
    from google.adk.workflow import START, Workflow

    def finish(value: JsonValue) -> WorkflowNodeResult:
        assert isinstance(value, bool)
        return WorkflowNodeResult(output={"approved": value})

    def factory(context: Any) -> App:
        return App(
            name="factory_app",
            root_agent=Workflow(
                name="approval_flow",
                edges=[
                    (
                        START,
                        context.function_nodes["approval"],
                        context.function_nodes["finish"],
                    )
                ],
                max_concurrency=1,
            ),
        )

    application = AdkApplicationSpec(
        agent_id="approval_flow",
        revision="v1",
        kind="graph",
        factory=factory,
        action_specs=(
            WorkflowActionSpec(
                code="confirm",
                kind="confirmation",
                response_schema={"type": "boolean"},
            ),
        ),
        function_nodes=(
            WorkflowRequestInputNodeSpec(
                name="approval",
                action_code="confirm",
            ),
            WorkflowTransformNodeSpec(
                name="finish",
                transform=finish,
                input_schema={"type": "boolean"},
                output_schema={"type": "object"},
            ),
        ),
        output_format="json",
        resumable=True,
    )
    backend = _SdkAdkBackend(
        api_key="secret",  # security-test-fixture
        model="model",
    )
    executor = ToolExecutor((), identity=AgentIdentity("tenant"))
    try:
        pending = await backend.invoke_workflow(
            application=application,
            invocation=WorkflowInvocation(session_id="session", text="begin"),
            app_name="approval_app",
            user_id="tenant",
            session_id="session",
            tool_executor=executor,
            max_llm_calls=1,
        )
        completed = await backend.invoke_workflow(
            application=application,
            invocation=WorkflowInvocation(
                session_id="session",
                resume_token=pending.resume_token,
                responses=(
                    WorkflowActionResponse(
                        runtime_action_id=pending.actions[0].runtime_action_id,
                        kind="confirmation",
                        value=True,
                    ),
                ),
            ),
            app_name="approval_app",
            user_id="tenant",
            session_id="session",
            tool_executor=executor,
            max_llm_calls=1,
        )
    finally:
        await backend.close()

    assert pending.state == "requires_action"
    assert pending.actions[0].code == "confirm"
    assert completed.state == "succeeded"
    assert completed.output == {"approved": True}


def test_sdk_workflow_resume_ignores_actions_already_answered_in_invocation() -> None:
    schema = {"type": "string"}
    application = AdkApplicationSpec(
        agent_id="sequential_actions",
        revision="v1",
        kind="team",
        factory=lambda context: object(),
        action_specs=(
            WorkflowActionSpec("first", "input", schema),
            WorkflowActionSpec("second", "input", schema),
        ),
        resumable=True,
    )

    def event(
        *,
        calls: list[Any] | None = None,
        responses: list[Any] | None = None,
        long_running_ids: set[str] | None = None,
    ) -> Any:
        return SimpleNamespace(
            invocation_id="invocation",
            long_running_tool_ids=long_running_ids or set(),
            get_function_calls=lambda: calls or [],
            get_function_responses=lambda: responses or [],
        )

    first_call = SimpleNamespace(
        id="action-1",
        name="adk_request_input",
        args={"payload": {"code": "first"}, "response_schema": schema},
    )
    first_response = SimpleNamespace(id="action-1", name="adk_request_input")
    second_call = SimpleNamespace(
        id="action-2",
        name="adk_request_input",
        args={"payload": {"code": "second"}, "response_schema": schema},
    )
    session = SimpleNamespace(
        events=[
            event(calls=[first_call], long_running_ids={"action-1"}),
            event(responses=[first_response]),
            event(calls=[second_call], long_running_ids={"action-2"}),
        ]
    )

    backend = object.__new__(_SdkAdkBackend)
    pending = backend._pending_actions_for_resume(
        application,
        session,
        invocation_id="invocation",
    )

    assert tuple(pending) == ("action-2",)


@pytest.mark.asyncio
async def test_sdk_team_accepts_only_bound_adk_agent_control_tools() -> None:
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.tools import request_input

    backend = _SdkAdkBackend(
        api_key="secret",  # security-test-fixture
        model="model",
    )
    coordinator_instruction = ["coordinate"]

    def factory(context: Any) -> App:
        task = LlmAgent(
            name="research_task",
            model=context.model,
            mode="task",
            tools=[request_input],
        )
        single = LlmAgent(
            name="formatter",
            model=context.model,
            mode="single_turn",
        )
        return App(
            name="factory_team",
            root_agent=LlmAgent(
                name="coordinator",
                model=context.model,
                mode="chat",
                instruction=coordinator_instruction[0],
                sub_agents=[task, single],
            ),
        )

    application = AdkApplicationSpec(
        agent_id="research_team",
        revision="v1",
        kind="team",
        factory=factory,
        action_specs=(
            WorkflowActionSpec(
                code="details",
                kind="input",
                response_schema={"type": "string"},
            ),
        ),
        resumable=True,
    )
    executor = ToolExecutor((), identity=AgentIdentity("tenant"))

    app, root_name, terminal_name, control_names = backend._build_workflow_app(
        application,
        app_name="internal_team",
        tool_executor=executor,
    )

    assert app.resumability_config is not None
    assert root_name == "coordinator"
    assert terminal_name is None
    assert control_names == frozenset({"research_task", "formatter"})
    coordinator_instruction[0] = "changed definition"
    with pytest.raises(AiProviderError, match="topology changed"):
        backend._build_workflow_app(
            application,
            app_name="internal_team",
            tool_executor=executor,
        )
    await backend.close()


def test_sdk_workflow_rejects_cycles_multiple_terminals_and_function_nodes() -> None:
    from google.adk.workflow import START, FunctionNode, JoinNode, Workflow

    first = SimpleNamespace(name="first")
    second = SimpleNamespace(name="second")
    cyclic = SimpleNamespace(
        nodes=[START, first, second],
        edges=[
            SimpleNamespace(from_node=START, to_node=first, route=None),
            SimpleNamespace(from_node=first, to_node=second, route="forward"),
            SimpleNamespace(from_node=second, to_node=first, route="back"),
        ],
    )
    with pytest.raises(AiProviderError, match="must be acyclic"):
        _SdkAdkBackend._validate_graph_shape(cyclic, start=START)

    multiple_terminals = SimpleNamespace(
        nodes=[START, first, second],
        edges=[
            SimpleNamespace(from_node=START, to_node=first, route="first"),
            SimpleNamespace(from_node=START, to_node=second, route="second"),
        ],
    )
    with pytest.raises(AiProviderError, match="one terminal"):
        _SdkAdkBackend._validate_graph_shape(multiple_terminals, start=START)

    terminal = SimpleNamespace(name="terminal")
    unreachable = SimpleNamespace(
        nodes=[START, first, second, terminal],
        edges=[
            SimpleNamespace(from_node=START, to_node=first, route=None),
            SimpleNamespace(from_node=first, to_node=terminal, route=None),
            SimpleNamespace(from_node=second, to_node=terminal, route=None),
        ],
    )
    with pytest.raises(AiProviderError, match="unreachable"):
        _SdkAdkBackend._validate_graph_shape(unreachable, start=START)

    def transform(node_input: Any) -> Any:
        return node_input

    function_node = FunctionNode(
        func=transform,
        name="transform",
        parameter_binding="node_input",
    )
    workflow = Workflow(
        name="function_flow",
        edges=[(START, function_node)],
        max_concurrency=1,
    )
    backend = object.__new__(_SdkAdkBackend)
    backend._max_workflow_concurrency = 4
    backend._model = object()
    with pytest.raises(AiProviderError, match="FunctionNode is not allowlisted"):
        backend._validate_graph_application(workflow, adapter_names={})

    application = AdkApplicationSpec(
        agent_id="unused_function",
        revision="v1",
        kind="graph",
        factory=lambda context: object(),
        function_nodes=(
            WorkflowTransformNodeSpec(
                name="declared",
                transform=lambda value: WorkflowNodeResult(output=value),
            ),
        ),
    )
    _, managed_specs = backend._build_workflow_function_nodes(application)
    managed_terminal = JoinNode(name="terminal")
    managed_missing = Workflow(
        name="managed_missing",
        edges=[(START, managed_terminal)],
        max_concurrency=1,
    )
    with pytest.raises(AiProviderError, match="must all be used"):
        backend._validate_graph_application(
            managed_missing,
            adapter_names={},
            function_specs=managed_specs,
        )


def test_sdk_workflow_topology_tracks_execution_controls() -> None:
    from google.adk.agents import LlmAgent
    from google.adk.workflow import JoinNode, RetryConfig

    first = JoinNode(name="first", timeout=1, rerun_on_resume=False)
    second = JoinNode(
        name="second",
        timeout=2,
        rerun_on_resume=True,
        retry_config=RetryConfig(max_attempts=2),
    )

    assert _SdkAdkBackend._node_execution_topology(
        first
    ) != _SdkAdkBackend._node_execution_topology(second)

    backend = object.__new__(_SdkAdkBackend)
    backend._model = object()
    parallel_agent = SimpleNamespace(
        model=backend._model,
        parallel_worker=True,
        tools=[],
    )
    with pytest.raises(AiProviderError, match="parallel agent"):
        backend._validate_workflow_agent(
            parallel_agent,
            adapter_names={},
            request_input_id=None,
        )

    callable_instruction = LlmAgent(
        name="callable_instruction",
        model="model",
        instruction=lambda context: "dynamic",
    )
    backend._model = callable_instruction.model
    with pytest.raises(AiProviderError, match="callable instructions"):
        backend._validate_workflow_agent(
            callable_instruction,
            adapter_names={},
            request_input_id=None,
        )

    callback_agent = LlmAgent(
        name="callback_agent",
        model="model",
        before_model_callback=lambda context, request: None,
    )
    backend._model = callback_agent.model
    with pytest.raises(AiProviderError, match="callbacks are unsupported"):
        backend._validate_workflow_agent(
            callback_agent,
            adapter_names={},
            request_input_id=None,
        )

    configured_tool_agent = LlmAgent(
        name="configured_tool_agent",
        model="model",
        mode="single_turn",
    )
    from google.genai import types

    configured_tool_agent.__dict__["generate_content_config"] = (
        types.GenerateContentConfig.model_construct(
            tools=[
                types.Tool.model_construct(
                    google_search=types.GoogleSearch.model_construct()
                )
            ]
        )
    )
    backend._model = configured_tool_agent.model
    with pytest.raises(AiProviderError, match="configured tools are unsupported"):
        backend._validate_workflow_agent(
            configured_tool_agent,
            adapter_names={},
            request_input_id=None,
        )


@pytest.mark.asyncio
async def test_sdk_workflow_rejects_mutated_managed_function_callable() -> None:
    from google.adk.apps import App
    from google.adk.workflow import START, Workflow

    def transform(value: JsonValue) -> WorkflowNodeResult:
        return WorkflowNodeResult(output=value)

    def factory(context: Any) -> App:
        node = context.function_nodes["transform"]
        node._func = lambda value: WorkflowNodeResult(output="injected")
        return App(
            name="mutated_function",
            root_agent=Workflow(
                name="mutated_function",
                edges=[(START, node)],
                max_concurrency=1,
            ),
        )

    application = AdkApplicationSpec(
        agent_id="mutated_function",
        revision="v1",
        kind="graph",
        factory=factory,
        function_nodes=(
            WorkflowTransformNodeSpec(name="transform", transform=transform),
        ),
    )
    backend = _SdkAdkBackend(
        api_key="secret",  # security-test-fixture
        model="model",
    )
    executor = ToolExecutor((), identity=AgentIdentity("tenant"))
    try:
        with pytest.raises(AiProviderError, match="FunctionNode was modified"):
            backend._build_workflow_app(
                application,
                app_name="mutated_function",
                tool_executor=executor,
            )
    finally:
        await backend.close()


def test_sdk_workflow_rejects_mutated_callable_definition_before_build() -> None:
    state = "original"

    def factory(context: Any) -> object:
        del context
        return {"state": state}

    application = AdkApplicationSpec(
        agent_id="mutated_definition",
        revision="v1",
        kind="graph",
        factory=factory,
    )
    state = "changed"
    backend = object.__new__(_SdkAdkBackend)

    with pytest.raises(AiProviderError, match="definition changed"):
        backend._build_workflow_app(
            application,
            app_name="mutated_definition",
            tool_executor=ToolExecutor((), identity=AgentIdentity("tenant")),
        )


def test_sdk_workflow_rejects_ambiguous_private_and_unsupported_actions() -> None:
    schema = {"type": "string"}
    application = AdkApplicationSpec(
        agent_id="team",
        revision="v1",
        kind="team",
        factory=lambda context: object(),
        action_specs=(
            WorkflowActionSpec("first", "input", schema),
            WorkflowActionSpec("second", "input", schema),
        ),
        resumable=True,
    )

    def event_for(args: dict[str, Any]) -> Any:
        function_call = SimpleNamespace(
            id="runtime-action",
            name="adk_request_input",
            args=args,
        )
        return SimpleNamespace(
            long_running_tool_ids={"runtime-action"},
            get_function_calls=lambda: [function_call],
        )

    mapped = _SdkAdkBackend._pending_actions_from_event(
        application,
        event_for({"message": "first", "response_schema": schema}),
    )
    assert [action.code for action in mapped] == ["first"]
    with pytest.raises(AiWorkflowActionUnsupportedError, match="unknown action"):
        _SdkAdkBackend._pending_actions_from_event(
            application,
            event_for({"message": "missing", "response_schema": schema}),
        )
    with pytest.raises(AiWorkflowActionUnsupportedError, match="invalid action"):
        _SdkAdkBackend._pending_actions_from_event(
            application,
            event_for(
                {
                    "payload": {"code": "first", "extra": "discarded"},
                    "response_schema": schema,
                }
            ),
        )

    auth_event = SimpleNamespace(
        actions=SimpleNamespace(
            requested_auth_configs={"runtime-action": object()},
            requested_tool_confirmations={},
        ),
        long_running_tool_ids=set(),
        get_function_calls=lambda: [],
    )
    with pytest.raises(AiWorkflowActionUnsupportedError, match="auth is unsupported"):
        _SdkAdkBackend._reject_unsupported_workflow_actions(auth_event)

    unknown_call = SimpleNamespace(
        id="runtime-action",
        name="unknown_long_running_action",
    )
    unknown_event = SimpleNamespace(
        actions=SimpleNamespace(
            requested_auth_configs={},
            requested_tool_confirmations={},
        ),
        long_running_tool_ids={"runtime-action"},
        get_function_calls=lambda: [unknown_call],
    )
    with pytest.raises(AiWorkflowActionUnsupportedError, match="unsupported actions"):
        _SdkAdkBackend._reject_unsupported_workflow_actions(unknown_event)

    delegated_event = SimpleNamespace(
        actions=SimpleNamespace(
            requested_auth_configs={},
            requested_tool_confirmations={},
        ),
        long_running_tool_ids=set(),
        get_function_calls=lambda: [
            SimpleNamespace(id="first", name="research_task"),
            SimpleNamespace(id="second", name="formatter"),
        ],
    )
    with pytest.raises(
        AiWorkflowActionUnsupportedError, match="parallel task delegation"
    ):
        _SdkAdkBackend._reject_unsupported_workflow_actions(
            delegated_event,
            agent_control_names=frozenset({"research_task", "formatter"}),
        )


def _minimal_sdk_backend(*, runner_cleanup_timeout_seconds: float) -> _SdkAdkBackend:
    class ExistingSessions:
        async def get_session(self, **kwargs: Any) -> object:
            return object()

    backend = object.__new__(_SdkAdkBackend)
    backend._sessions = ExistingSessions()
    backend._app_name = "app"
    backend._agent_name = "agent"
    backend._model = object()
    backend._model_name = "model"
    backend._runner_cleanups = {}
    backend._runner_cleanup_timeout_seconds = runner_cleanup_timeout_seconds
    return backend


def _patch_minimal_sdk_runner(
    monkeypatch: pytest.MonkeyPatch, runner_type: type[Any]
) -> None:
    from google.adk import agents, apps, runners, telemetry
    from google.adk.agents import run_config

    class FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeApp:
        def __init__(self, *, name: str, root_agent: FakeAgent) -> None:
            self.name = name
            self.root_agent = root_agent

    class FakeContentCapturingMode:
        NO_CONTENT = object()

    class FakeTelemetryConfig:
        def __init__(self, *, capture_message_content: object) -> None:
            self.capture_message_content = capture_message_content

    class FakeRunConfig:
        def __init__(
            self, *, max_llm_calls: int, telemetry: FakeTelemetryConfig
        ) -> None:
            self.max_llm_calls = max_llm_calls
            self.telemetry = telemetry

    monkeypatch.setattr(agents, "LlmAgent", FakeAgent)
    monkeypatch.setattr(apps, "App", FakeApp)
    monkeypatch.setattr(run_config, "RunConfig", FakeRunConfig)
    monkeypatch.setattr(runners, "Runner", runner_type)
    monkeypatch.setattr(telemetry, "ContentCapturingMode", FakeContentCapturingMode)
    monkeypatch.setattr(telemetry, "TelemetryConfig", FakeTelemetryConfig)


async def _run_minimal_sdk_backend(backend: _SdkAdkBackend) -> AdkResult:
    return await backend.run(
        system_instruction="",
        history=(),
        new_message=ChatMessage(role="user", content="hello"),
        user_id="user",
        session_id="session",
        tool_executor=ToolExecutor((), identity=AgentIdentity("user")),
        max_llm_calls=1,
    )


@pytest.mark.asyncio
async def test_runner_close_failure_does_not_override_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run_async(self, **kwargs: Any) -> AsyncIterator[Any]:
            async def events() -> AsyncIterator[Any]:
                if False:
                    yield None
                raise AiProviderError("original provider failure")

            return events()

        async def close(self) -> None:
            raise RuntimeError("runner cleanup detail")

    _patch_minimal_sdk_runner(monkeypatch, FailedRunner)
    backend = _minimal_sdk_backend(runner_cleanup_timeout_seconds=0.1)

    with pytest.raises(AiProviderError, match="original provider failure") as captured:
        await _run_minimal_sdk_backend(backend)

    assert captured.value.__cause__ is None
    assert "runner cleanup detail" not in str(captured.value)
    assert backend.has_unresolved_runner_cleanup is True


@pytest.mark.asyncio
async def test_runner_close_timeout_does_not_override_request_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_entered = asyncio.Event()
    close_entered = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run_async(self, **kwargs: Any) -> AsyncIterator[Any]:
            async def events() -> AsyncIterator[Any]:
                run_entered.set()
                await asyncio.Event().wait()
                if False:
                    yield None

            return events()

        async def close(self) -> None:
            close_entered.set()
            while not release_close.is_set():
                try:
                    await release_close.wait()
                except asyncio.CancelledError:
                    pass

    _patch_minimal_sdk_runner(monkeypatch, BlockingRunner)
    backend = _minimal_sdk_backend(runner_cleanup_timeout_seconds=0.01)
    task = asyncio.create_task(_run_minimal_sdk_backend(backend))
    await run_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)
    assert close_entered.is_set()
    assert backend.has_unresolved_runner_cleanup is True

    release_close.set()
    async with asyncio.timeout(0.2):
        while any(
            not cleanup.close_task.done()
            for cleanup in backend._runner_cleanups.values()
        ):
            await asyncio.sleep(0)
    assert backend.has_unresolved_runner_cleanup is False
