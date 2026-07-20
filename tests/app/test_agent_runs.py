from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import Request

from app.modules.agent_runs.api import (
    AgentRunAction,
    AgentRunConfirmationResponse,
    AgentRunCreateRequest,
    AgentRunInputResponse,
    AgentRunResumeRequest,
    AgentRunStatus,
)
from app.modules.agent_runs.module import AgentRunsModuleConfig
from app.routes.agent_runs import create_agent_runs_router
from app.services.agent_runs import (
    AgentRunConflictError,
    AgentRunNotFoundError,
    AgentRunPayloadError,
    AgentRunsCapacityError,
    AgentRunsShutdownError,
    AgentRunsUnavailableError,
    AppAgentRunsService,
)
from app.services.identity import AuthNotConfiguredError
from nexus_kernel.ai import (
    AdkApplicationRegistry,
    AdkApplicationSpec,
    AgentIdentity,
    AgentToolSpec,
    TokenUsage,
    WorkflowActionSpec,
    WorkflowInvocation,
    WorkflowInvocationResult,
    WorkflowPendingAction,
)
from nexus_kernel.ai.errors import AiWorkflowActionUnsupportedError


class FixedResolver:
    def __init__(self, principal_id: str = "tenant-a") -> None:
        self._identity = AgentIdentity(principal_id)

    async def resolve(self, request: Request) -> AgentIdentity:
        return self._identity


class MutableResolver:
    def __init__(self, principal_id: str = "tenant-a") -> None:
        self.principal_id = principal_id

    async def resolve(self, request: Request) -> AgentIdentity:
        return AgentIdentity(self.principal_id)


class FakeWorkflowRuntime:
    provider_id = "google_adk"

    def __init__(self, results: Sequence[WorkflowInvocationResult | Exception]) -> None:
        self._results = list(results)
        self.invocations: list[WorkflowInvocation] = []
        self.identities: list[AgentIdentity] = []
        self.deleted: list[str] = []
        self.block: asyncio.Event | None = None
        self.block_session_id: str | None = None
        self.delay_seconds = 0.0
        self.cancel_seen = asyncio.Event()
        self.fail_delete = False
        self.timeout_seconds: list[float | None] = []

    async def invoke(
        self,
        application: AdkApplicationSpec,
        invocation: WorkflowInvocation,
        *,
        identity: AgentIdentity,
        tools: Sequence[AgentToolSpec] = (),
        timeout_seconds: float | None = None,
    ) -> WorkflowInvocationResult:
        self.invocations.append(invocation)
        self.identities.append(identity)
        self.timeout_seconds.append(timeout_seconds)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.block is not None and (
            self.block_session_id is None
            or self.block_session_id == invocation.session_id
        ):
            try:
                await self.block.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.block.wait()
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def delete_session(
        self,
        application: AdkApplicationSpec,
        *,
        identity: AgentIdentity,
        session_id: str,
    ) -> None:
        self.deleted.append(session_id)
        if self.fail_delete:
            raise RuntimeError("private cleanup failure")


class ExplodingApplyService(AppAgentRunsService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._explode_once = True

    async def _apply_result(
        self, run_id: str, result: WorkflowInvocationResult
    ) -> None:
        if self._explode_once:
            self._explode_once = False
            raise RuntimeError("private phase failure")
        await super()._apply_result(run_id, result)


class PausingApplyService(AppAgentRunsService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.apply_entered = asyncio.Event()
        self.release_apply = asyncio.Event()

    async def _apply_result(
        self, run_id: str, result: WorkflowInvocationResult
    ) -> None:
        self.apply_entered.set()
        await self.release_apply.wait()
        await super()._apply_result(run_id, result)


class PausingAdmissionService(AppAgentRunsService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.admission_entered = asyncio.Event()
        self.release_admission = asyncio.Event()
        self._pause_once = True

    async def _prepare_session_for_run(self, *args: Any, **kwargs: Any) -> None:
        if self._pause_once:
            self._pause_once = False
            self.admission_entered.set()
            await self.release_admission.wait()
        await super()._prepare_session_for_run(*args, **kwargs)


class CountingDrainService(AppAgentRunsService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.drain_calls = 0

    async def drain(self, *, timeout_seconds: float | None = None) -> bool:
        self.drain_calls += 1
        return await super().drain(timeout_seconds=timeout_seconds)


class BlockingDeleteRuntime(FakeWorkflowRuntime):
    def __init__(self, results: Sequence[WorkflowInvocationResult | Exception]) -> None:
        super().__init__(results)
        self.delete_entered = asyncio.Event()
        self.release_delete = asyncio.Event()

    async def delete_session(
        self,
        application: AdkApplicationSpec,
        *,
        identity: AgentIdentity,
        session_id: str,
    ) -> None:
        self.deleted.append(session_id)
        self.delete_entered.set()
        await self.release_delete.wait()


class RecordingObserver:
    def __init__(self, *, fail_runtime: bool = False) -> None:
        self.fail_runtime = fail_runtime
        self.runtime: list[dict[str, object]] = []
        self.transitions: list[str] = []
        self.terminals: list[str] = []
        self.phases: list[tuple[str, float]] = []
        self.capacity_rejections = 0

    def set_agent_runs_runtime(
        self,
        *,
        queue_depth: int,
        active_phases: int,
        waiting_actions: int,
        cleanup_debt: int,
        runs_by_status: Any,
    ) -> None:
        if self.fail_runtime:
            raise RuntimeError("private observer failure")
        self.runtime.append(
            {
                "queue_depth": queue_depth,
                "active_phases": active_phases,
                "waiting_actions": waiting_actions,
                "cleanup_debt": cleanup_debt,
                "runs_by_status": dict(runs_by_status),
            }
        )

    def record_agent_run_transition(self, status: str) -> None:
        self.transitions.append(status)

    def record_agent_run_terminal(self, status: str) -> None:
        self.terminals.append(status)

    def record_agent_run_phase(self, outcome: str, duration_seconds: float) -> None:
        self.phases.append((outcome, duration_seconds))

    def record_agent_runs_capacity_rejection(self) -> None:
        self.capacity_rejections += 1


def _factory(context: object) -> object:
    return object()


def _allow_application(
    identity: AgentIdentity, application: AdkApplicationSpec
) -> bool:
    del identity, application
    return True


def _service(*args: Any, **kwargs: Any) -> AppAgentRunsService:
    kwargs.setdefault("authorizer", _allow_application)
    return AppAgentRunsService(*args, **kwargs)


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/"})


def _config(**overrides: Any) -> AgentRunsModuleConfig:
    values: dict[str, Any] = {
        "max_entries": 8,
        "max_concurrent_phases": 2,
        "phase_timeout_seconds": 1.0,
        "overall_lifetime_seconds": 5.0,
        "action_timeout_seconds": 2.0,
        "terminal_retention_seconds": 5.0,
        "max_resumes": 2,
        "max_payload_bytes": 4096,
        "poll_interval_seconds": 0.01,
    }
    values.update(overrides)
    return AgentRunsModuleConfig.model_validate(values)


@pytest.mark.parametrize(
    "ttl", [True, 0.0, -1.0, float("nan"), float("inf"), float("-inf")]
)
def test_agent_runs_service_rejects_non_positive_finite_session_ttl(
    ttl: object,
) -> None:
    application = AdkApplicationSpec("planner", "v1", "graph", _factory)
    with pytest.raises(ValueError, match="positive finite"):
        _service(
            FakeWorkflowRuntime(()),
            AdkApplicationRegistry((application,)),
            _config(),
            identity_resolver=FixedResolver(),
            session_ttl_seconds=ttl,  # type: ignore[arg-type]
        )


async def _terminal(service: AppAgentRunsService, run_id: str) -> Any:
    for _ in range(100):
        snapshot = await service.get_run(run_id, _request())
        if snapshot.status in {
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELED,
            AgentRunStatus.EXPIRED,
            AgentRunStatus.REQUIRES_ACTION,
        }:
            return snapshot
        await asyncio.sleep(0.005)
    raise AssertionError("run did not reach an observable state")


async def _wait_until_deleted(runtime: FakeWorkflowRuntime, session_id: str) -> None:
    for _ in range(100):
        if session_id in runtime.deleted:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("session was not deleted")


def _route_endpoint(router: Any, path: str, method: str) -> Any:
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", None) == path
        and method in getattr(route, "methods", set())
    )


def test_public_action_accepts_draft_2020_12_boolean_schema() -> None:
    action = AgentRunAction(
        action_id="action",
        code="deny_input",
        type="input",
        response_schema=False,
        expires_at=datetime.now(UTC),
    )

    assert action.response_schema is False


def test_agent_run_routes_publish_actual_success_statuses_and_schemas() -> None:
    router = create_agent_runs_router(cast(Any, object()))
    routes = {(route.path, next(iter(route.methods))): route for route in router.routes}

    create = routes[("/api/v1/agents/{agent_id}/runs", "POST")]
    resume = routes[("/api/v1/agent-runs/{run_id}/resume", "POST")]
    cancel = routes[("/api/v1/agent-runs/{run_id}", "DELETE")]
    session_delete = routes[
        ("/api/v1/agents/{agent_id}/sessions/{session_id}", "DELETE")
    ]

    assert create.status_code == 202
    assert resume.status_code == 202
    assert create.response_model is not None
    assert resume.response_model is not None
    assert cancel.response_model is not None
    assert cancel.responses[202]["model"] is not None
    assert session_delete.response_model is not None


@pytest.mark.asyncio
async def test_run_is_idempotent_and_successful_session_can_continue() -> None:
    application = AdkApplicationSpec("writer", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="succeeded",
                output="first",
                usage=TokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
                model="model-v1",
            ),
            WorkflowInvocationResult(state="succeeded", output="second"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "writer",
            AgentRunCreateRequest(input="private input", session_id="conversation"),
            _request(),
            idempotency_key="create-one",
        )
        replay = await service.create_run(
            "writer",
            AgentRunCreateRequest(input="private input", session_id="conversation"),
            _request(),
            idempotency_key="create-one",
        )
        assert replay.run_id == created.run_id
        with pytest.raises(AgentRunConflictError) as idempotency_conflict:
            await service.create_run(
                "writer",
                AgentRunCreateRequest(input="different", session_id="conversation"),
                _request(),
                idempotency_key="create-one",
            )
        assert idempotency_conflict.value.code == "idempotency_conflict"

        with pytest.raises(AgentRunPayloadError) as payload_error:
            await service.create_run(
                "writer",
                AgentRunCreateRequest(input="x" * 5000),
                _request(),
                idempotency_key="oversized",
            )
        assert payload_error.value.code == "agent_payload_too_large"

        first = await _terminal(service, created.run_id)
        assert first.status is AgentRunStatus.SUCCEEDED
        assert first.output == "first"
        continuation = await service.create_run(
            "writer",
            AgentRunCreateRequest(
                input="next", session_action="continue", session_id="conversation"
            ),
            _request(),
            idempotency_key="create-two",
        )
        second = await _terminal(service, continuation.run_id)
        assert second.output == "second"
        assert [item.session_id for item in runtime.invocations] == [
            "conversation",
            "conversation",
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_structured_action_is_allowlisted_and_internal_ids_are_hidden() -> None:
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
    application = AdkApplicationSpec(
        "research_team",
        "v1",
        "team",
        _factory,
        action_specs=(action_spec,),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                usage=TokenUsage(input_tokens=2, output_tokens=1, total_tokens=4),
                actions=(
                    WorkflowPendingAction(
                        runtime_action_id="internal-action",
                        code="provide_details",
                        kind="input",
                    ),
                ),
                resume_token="internal-resume-token",  # security-test-fixture
            ),
            WorkflowInvocationResult(
                state="succeeded",
                output="complete",
                usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=6),
            ),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "research_team",
            AgentRunCreateRequest(input="start"),
            _request(),
            idempotency_key="action-create",
        )
        pending = await _terminal(service, created.run_id)
        assert pending.status is AgentRunStatus.REQUIRES_ACTION
        rendered = pending.model_dump_json()
        assert "internal-action" not in rendered
        assert "internal-resume-token" not in rendered
        assert pending.actions[0].response_schema["type"] == "object"
        assert pending.usage == TokenUsage(
            input_tokens=2, output_tokens=1, total_tokens=4
        )

        with pytest.raises(AgentRunConflictError) as session_busy:
            await service.create_run(
                "research_team",
                AgentRunCreateRequest(
                    input="continue",
                    session_action="continue",
                    session_id=pending.session_id,
                ),
                _request(),
                idempotency_key="busy-session",
            )
        assert session_busy.value.code == "agent_session_busy"

        with pytest.raises(AgentRunConflictError) as version_conflict:
            await service.resume_run(
                created.run_id,
                AgentRunResumeRequest(
                    version=pending.version + 1,
                    actions=[
                        AgentRunInputResponse(
                            type="input",
                            action_id=pending.actions[0].action_id,
                            value={"answer": "safe"},
                        )
                    ],
                ),
                _request(),
                idempotency_key="wrong-version",
            )
        assert version_conflict.value.code == "agent_run_version_conflict"

        resumed = await service.resume_run(
            created.run_id,
            AgentRunResumeRequest(
                version=pending.version,
                actions=[
                    AgentRunInputResponse(
                        type="input",
                        action_id=pending.actions[0].action_id,
                        value={"answer": "safe"},
                    )
                ],
            ),
            _request(),
            idempotency_key="action-resume",
        )
        assert resumed.status is AgentRunStatus.QUEUED
        completed = await _terminal(service, created.run_id)
        assert completed.status is AgentRunStatus.SUCCEEDED
        assert completed.usage == TokenUsage(
            input_tokens=5, output_tokens=3, total_tokens=10
        )
        response = runtime.invocations[1].responses[0]
        assert response.runtime_action_id == "internal-action"
        assert response.value["answer"] == "safe"

        with pytest.raises(AgentRunConflictError) as not_waiting:
            await service.resume_run(
                created.run_id,
                AgentRunResumeRequest(
                    version=completed.version,
                    actions=[
                        AgentRunInputResponse(
                            type="input",
                            action_id=pending.actions[0].action_id,
                            value={"answer": "safe"},
                        )
                    ],
                ),
                _request(),
                idempotency_key="not-waiting",
            )
        assert not_waiting.value.code == "agent_run_not_waiting"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_capacity_and_cancel_wait_for_runtime_and_cleanup_convergence() -> None:
    application = AdkApplicationSpec("worker", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="late"),)
    )
    runtime.block = asyncio.Event()
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(max_entries=1, max_concurrent_phases=1),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "worker",
            AgentRunCreateRequest(input="start"),
            _request(),
            idempotency_key="capacity-one",
        )
        for _ in range(100):
            running = await service.get_run(created.run_id, _request())
            if running.status is AgentRunStatus.RUNNING:
                break
            await asyncio.sleep(0.005)
        router = create_agent_runs_router(service)
        get_endpoint = _route_endpoint(router, "/api/v1/agent-runs/{run_id}", "GET")
        cancel_endpoint = _route_endpoint(
            router, "/api/v1/agent-runs/{run_id}", "DELETE"
        )
        running_response = await get_endpoint(created.run_id, _request())
        assert running_response.headers["Retry-After"] == "1"

        with pytest.raises(AgentRunsCapacityError) as capacity_error:
            await service.create_run(
                "worker",
                AgentRunCreateRequest(input="second"),
                _request(),
                idempotency_key="capacity-two",
            )
        assert capacity_error.value.code == "agent_run_capacity_exceeded"

        canceling_response = await cancel_endpoint(created.run_id, _request())
        assert canceling_response.status_code == 202
        await runtime.cancel_seen.wait()
        still_canceling = await service.get_run(created.run_id, _request())
        assert still_canceling.status is AgentRunStatus.CANCELING
        runtime.block.set()
        terminal = await _terminal(service, created.run_id)
        assert terminal.status is AgentRunStatus.CANCELED
        assert runtime.deleted == [created.session_id]
        terminal_response = await get_endpoint(created.run_id, _request())
        assert "Retry-After" not in terminal_response.headers
        repeated_cancel = await cancel_endpoint(created.run_id, _request())
        assert repeated_cancel.status_code == 200
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_idle_sessions_use_ttl_and_lru_without_evicting_busy_sessions() -> None:
    application = AdkApplicationSpec("planner", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(state="succeeded", output="one"),
            WorkflowInvocationResult(state="succeeded", output="two"),
            WorkflowInvocationResult(state="succeeded", output="three"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(
            max_entries=2,
            max_concurrent_phases=1,
            terminal_retention_seconds=0.01,
        ),
        identity_resolver=FixedResolver(),
        session_ttl_seconds=10.0,
    )
    await service.start()
    try:
        first = await service.create_run(
            "planner",
            AgentRunCreateRequest(input="one", session_id="session-one"),
            _request(),
            idempotency_key="lru-one",
        )
        await _terminal(service, first.run_id)
        await asyncio.sleep(0.01)
        second = await service.create_run(
            "planner",
            AgentRunCreateRequest(input="two", session_id="session-two"),
            _request(),
            idempotency_key="lru-two",
        )
        await _terminal(service, second.run_id)
        for _ in range(100):
            try:
                await service.get_run(first.run_id, _request())
            except AgentRunNotFoundError:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("terminal run was not pruned")

        third = await service.create_run(
            "planner",
            AgentRunCreateRequest(input="three", session_id="session-three"),
            _request(),
            idempotency_key="lru-three",
        )
        await _terminal(service, third.run_id)
        assert runtime.deleted[0] == "session-one"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_idle_session_ttl_deletes_backend_but_waiting_action_is_pinned() -> None:
    plain = AdkApplicationSpec("plain", "v1", "graph", _factory)
    action_spec = WorkflowActionSpec(
        code="confirm",
        kind="confirmation",
        response_schema={"type": "boolean"},
    )
    waiting = AdkApplicationSpec(
        "waiting",
        "v1",
        "team",
        _factory,
        action_specs=(action_spec,),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(state="succeeded", output="done"),
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction("runtime-id", "confirm", "confirmation"),
                ),
                resume_token="resume-state",  # security-test-fixture
            ),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((plain, waiting)),
        _config(),
        identity_resolver=FixedResolver(),
        session_ttl_seconds=0.02,
    )
    await service.start()
    try:
        completed = await service.create_run(
            "plain",
            AgentRunCreateRequest(input="done", session_id="idle-session"),
            _request(),
            idempotency_key="ttl-complete",
        )
        await _terminal(service, completed.run_id)
        pending = await service.create_run(
            "waiting",
            AgentRunCreateRequest(input="wait", session_id="pinned-session"),
            _request(),
            idempotency_key="ttl-waiting",
        )
        waiting_snapshot = await _terminal(service, pending.run_id)
        assert waiting_snapshot.status is AgentRunStatus.REQUIRES_ACTION
        await _wait_until_deleted(runtime, "idle-session")
        await asyncio.sleep(0.03)
        assert "pinned-session" not in runtime.deleted
        await service.cancel_run(pending.run_id, _request())
        await _terminal(service, pending.run_id)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_session_cleanup_failure_makes_close_retryable() -> None:
    application = AdkApplicationSpec("closer", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="done"),)
    )
    service = CountingDrainService(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
        authorizer=_allow_application,
    )
    await service.start()
    created = await service.create_run(
        "closer",
        AgentRunCreateRequest(input="done", session_id="close-session"),
        _request(),
        idempotency_key="close-run",
    )
    await _terminal(service, created.run_id)
    runtime.fail_delete = True
    with pytest.raises(AgentRunsShutdownError):
        await service.close()
    assert service.drain_calls == 1
    runtime.fail_delete = False
    await service.close()
    assert service.drain_calls == 1


@pytest.mark.asyncio
async def test_resume_limit_fails_without_exposing_unrecoverable_action() -> None:
    action_spec = WorkflowActionSpec(
        code="confirm",
        kind="confirmation",
        response_schema={"type": "boolean"},
    )
    application = AdkApplicationSpec(
        "limited",
        "v1",
        "team",
        _factory,
        action_specs=(action_spec,),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                actions=(
                    WorkflowPendingAction("runtime-one", "confirm", "confirmation"),
                ),
                resume_token="resume-one",  # security-test-fixture
            ),
            WorkflowInvocationResult(
                state="requires_action",
                usage=TokenUsage(input_tokens=2, output_tokens=1, total_tokens=4),
                actions=(
                    WorkflowPendingAction("runtime-two", "confirm", "confirmation"),
                ),
                resume_token="resume-two",  # security-test-fixture
            ),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(max_resumes=1),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "limited",
            AgentRunCreateRequest(input="start", session_id="limited-session"),
            _request(),
            idempotency_key="limit-create",
        )
        pending = await _terminal(service, created.run_id)
        await service.resume_run(
            created.run_id,
            AgentRunResumeRequest(
                version=pending.version,
                actions=[
                    AgentRunConfirmationResponse(
                        type="confirmation",
                        action_id=pending.actions[0].action_id,
                        confirmed=True,
                    )
                ],
            ),
            _request(),
            idempotency_key="limit-resume",
        )
        failed = await _terminal(service, created.run_id)

        assert failed.status is AgentRunStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "agent_resume_limit_exceeded"
        assert failed.actions == ()
        assert failed.usage == TokenUsage(
            input_tokens=3, output_tokens=2, total_tokens=6
        )
        assert runtime.deleted == ["limited-session"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_queued_resume_cancel_cleans_touched_session() -> None:
    action_spec = WorkflowActionSpec(
        code="confirm",
        kind="confirmation",
        response_schema={"type": "boolean"},
    )
    application = AdkApplicationSpec(
        "queued_resume",
        "v1",
        "team",
        _factory,
        action_specs=(action_spec,),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction("runtime-id", "confirm", "confirmation"),
                ),
                resume_token="resume-id",  # security-test-fixture
            ),
            WorkflowInvocationResult(state="succeeded", output="blocker done"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(max_concurrent_phases=1),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        target = await service.create_run(
            "queued_resume",
            AgentRunCreateRequest(input="target", session_id="target-session"),
            _request(),
            idempotency_key="queued-target",
        )
        pending = await _terminal(service, target.run_id)

        runtime.block = asyncio.Event()
        runtime.block_session_id = "blocker-session"
        blocker = await service.create_run(
            "queued_resume",
            AgentRunCreateRequest(input="block", session_id="blocker-session"),
            _request(),
            idempotency_key="queued-blocker",
        )
        for _ in range(100):
            running = await service.get_run(blocker.run_id, _request())
            if running.status is AgentRunStatus.RUNNING:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("blocker did not start")

        await service.resume_run(
            target.run_id,
            AgentRunResumeRequest(
                version=pending.version,
                actions=[
                    AgentRunConfirmationResponse(
                        type="confirmation",
                        action_id=pending.actions[0].action_id,
                        confirmed=True,
                    )
                ],
            ),
            _request(),
            idempotency_key="queued-resume",
        )
        canceling = await service.cancel_run(target.run_id, _request())
        assert canceling.status is AgentRunStatus.CANCELING
        await _wait_until_deleted(runtime, "target-session")
        canceled = await _terminal(service, target.run_id)
        assert canceled.status is AgentRunStatus.CANCELED
        assert [call.session_id for call in runtime.invocations] == [
            "target-session",
            "blocker-session",
        ]

        runtime.block.set()
        await _terminal(service, blocker.run_id)
    finally:
        if runtime.block is not None:
            runtime.block.set()
        await service.close()


@pytest.mark.asyncio
async def test_run_access_rechecks_authorizer_outside_service_lock() -> None:
    action_spec = WorkflowActionSpec(
        code="confirm",
        kind="confirmation",
        response_schema={"type": "boolean"},
    )
    application = AdkApplicationSpec(
        "revocable",
        "v1",
        "team",
        _factory,
        action_specs=(action_spec,),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction("runtime-id", "confirm", "confirmation"),
                ),
                resume_token="resume-id",  # security-test-fixture
            ),
        )
    )
    allowed = True
    service: AppAgentRunsService

    async def authorizer(
        identity: AgentIdentity, definition: AdkApplicationSpec
    ) -> bool:
        assert not service._lock.locked()
        return allowed

    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
        authorizer=authorizer,
    )
    await service.start()
    try:
        created = await service.create_run(
            "revocable",
            AgentRunCreateRequest(input="start"),
            _request(),
            idempotency_key="auth-create",
        )
        pending = await _terminal(service, created.run_id)
        allowed = False

        with pytest.raises(AgentRunNotFoundError):
            await service.get_run(created.run_id, _request())
        with pytest.raises(AgentRunNotFoundError):
            await service.resume_run(
                created.run_id,
                AgentRunResumeRequest(
                    version=pending.version,
                    actions=[
                        AgentRunConfirmationResponse(
                            type="confirmation",
                            action_id=pending.actions[0].action_id,
                            confirmed=True,
                        )
                    ],
                ),
                _request(),
                idempotency_key="auth-resume",
            )
        with pytest.raises(AgentRunNotFoundError):
            await service.cancel_run(created.run_id, _request())

        allowed = True
        await service.cancel_run(created.run_id, _request())
        await _terminal(service, created.run_id)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_overall_deadline_caps_phase_and_prevents_late_success() -> None:
    application = AdkApplicationSpec("deadline", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="too late"),)
    )
    runtime.delay_seconds = 0.1
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(
            phase_timeout_seconds=0.05,
            overall_lifetime_seconds=0.05,
            action_timeout_seconds=0.02,
        ),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "deadline",
            AgentRunCreateRequest(input="start", session_id="deadline-session"),
            _request(),
            idempotency_key="deadline-create",
        )
        expired = await _terminal(service, created.run_id)
        assert expired.status is AgentRunStatus.EXPIRED
        assert expired.error is not None
        assert expired.error.code == "agent_run_expired"
        assert expired.output is None
        assert runtime.deleted == ["deadline-session"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_payload_encoding_failures_are_stable_and_do_not_kill_worker() -> None:
    application = AdkApplicationSpec("encoding", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(state="succeeded", output="\ud800"),
            WorkflowInvocationResult(state="succeeded", output="safe"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(max_concurrent_phases=1),
        identity_resolver=FixedResolver(),
    )
    recursive: list[Any] = []
    recursive.append(recursive)
    with pytest.raises(AgentRunPayloadError):
        service._validate_payload(recursive)
    with pytest.raises(AgentRunPayloadError):
        service._validate_payload("\ud800")

    await service.start()
    try:
        invalid = await service.create_run(
            "encoding",
            AgentRunCreateRequest(input="first", session_id="invalid-output"),
            _request(),
            idempotency_key="invalid-output",
        )
        failed = await _terminal(service, invalid.run_id)
        assert failed.status is AgentRunStatus.FAILED
        assert failed.output is None
        assert runtime.deleted == ["invalid-output"]

        valid = await service.create_run(
            "encoding",
            AgentRunCreateRequest(input="second", session_id="valid-output"),
            _request(),
            idempotency_key="valid-output",
        )
        succeeded = await _terminal(service, valid.run_id)
        assert succeeded.status is AgentRunStatus.SUCCEEDED
        assert succeeded.output == "safe"
        assert not service._workers[0].done()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unexpected_phase_failure_is_isolated_and_worker_continues() -> None:
    application = AdkApplicationSpec("resilient", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(state="succeeded", output="discarded"),
            WorkflowInvocationResult(state="succeeded", output="kept"),
        )
    )
    service = ExplodingApplyService(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(max_concurrent_phases=1),
        identity_resolver=FixedResolver(),
        authorizer=_allow_application,
    )
    await service.start()
    try:
        broken = await service.create_run(
            "resilient",
            AgentRunCreateRequest(input="first", session_id="broken-phase"),
            _request(),
            idempotency_key="broken-phase",
        )
        failed = await _terminal(service, broken.run_id)
        assert failed.status is AgentRunStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "agent_phase_failed"
        assert runtime.deleted == ["broken-phase"]

        healthy = await service.create_run(
            "resilient",
            AgentRunCreateRequest(input="second", session_id="healthy-phase"),
            _request(),
            idempotency_key="healthy-phase",
        )
        succeeded = await _terminal(service, healthy.run_id)
        assert succeeded.status is AgentRunStatus.SUCCEEDED
        assert succeeded.output == "kept"
        assert not service._workers[0].done()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_concurrent_idempotent_create_replays_before_session_admission() -> None:
    application = AdkApplicationSpec("idempotent", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="done"),)
    )
    service = PausingAdmissionService(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
        authorizer=_allow_application,
    )
    await service.start()
    request = AgentRunCreateRequest(input="same", session_id="same-session")
    try:
        first_task = asyncio.create_task(
            service.create_run(
                "idempotent",
                request,
                _request(),
                idempotency_key="same-key",
            )
        )
        await service.admission_entered.wait()
        second_task = asyncio.create_task(
            service.create_run(
                "idempotent",
                request,
                _request(),
                idempotency_key="same-key",
            )
        )
        await asyncio.sleep(0)
        service.release_admission.set()
        first, second = await asyncio.gather(first_task, second_task)

        assert first.run_id == second.run_id
        assert first.session_id == second.session_id == "same-session"
        await _terminal(service, first.run_id)
        assert len(runtime.invocations) == 1
    finally:
        service.release_admission.set()
        await service.close()


@pytest.mark.asyncio
async def test_unsupported_workflow_action_has_stable_failure_code() -> None:
    application = AdkApplicationSpec("unsupported", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (
            AiWorkflowActionUnsupportedError("private backend detail"),
            WorkflowInvocationResult(state="succeeded", output="safe"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(max_concurrent_phases=1),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        unsupported = await service.create_run(
            "unsupported",
            AgentRunCreateRequest(input="first", session_id="unsupported-action"),
            _request(),
            idempotency_key="unsupported-action",
        )
        failed = await _terminal(service, unsupported.run_id)
        assert failed.status is AgentRunStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "agent_action_unsupported"
        assert runtime.deleted == ["unsupported-action"]

        supported = await service.create_run(
            "unsupported",
            AgentRunCreateRequest(input="second", session_id="supported-action"),
            _request(),
            idempotency_key="supported-action",
        )
        succeeded = await _terminal(service, supported.run_id)
        assert succeeded.status is AgentRunStatus.SUCCEEDED
        assert succeeded.output == "safe"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_run_cleanup_is_keyed_across_cancel_and_worker_paths() -> None:
    application = AdkApplicationSpec(
        "cleanup_race",
        "v1",
        "graph",
        _factory,
        action_specs=(
            WorkflowActionSpec("confirm", "confirmation", {"type": "boolean"}),
        ),
        resumable=True,
    )
    runtime = BlockingDeleteRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction("runtime-action", "confirm", "confirmation"),
                ),
                resume_token="resume-token",  # security-test-fixture
            ),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "cleanup_race",
            AgentRunCreateRequest(input="start", session_id="cleanup-race"),
            _request(),
            idempotency_key="cleanup-race",
        )
        waiting = await _terminal(service, created.run_id)
        assert waiting.status is AgentRunStatus.REQUIRES_ACTION

        canceling = await service.cancel_run(created.run_id, _request())
        await runtime.delete_entered.wait()
        competing = asyncio.create_task(
            service._cleanup_run(created.run_id, AgentRunStatus.EXPIRED)
        )
        runtime.release_delete.set()
        await competing

        canceled = await _terminal(service, created.run_id)
        assert canceled.status is AgentRunStatus.CANCELED
        assert canceled.version == canceling.version + 1
        assert runtime.deleted == ["cleanup-race"]
    finally:
        runtime.release_delete.set()
        await service.close()


@pytest.mark.asyncio
async def test_too_many_pending_actions_fail_before_requires_action() -> None:
    application = AdkApplicationSpec(
        "action_limit",
        "v1",
        "graph",
        _factory,
        action_specs=(
            WorkflowActionSpec("confirm", "confirmation", {"type": "boolean"}),
        ),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=tuple(
                    WorkflowPendingAction(
                        f"runtime-action-{index}", "confirm", "confirmation"
                    )
                    for index in range(129)
                ),
                resume_token="resume-token",  # security-test-fixture
            ),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "action_limit",
            AgentRunCreateRequest(input="start", session_id="action-limit"),
            _request(),
            idempotency_key="action-limit",
        )
        failed = await _terminal(service, created.run_id)
        assert failed.status is AgentRunStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "agent_action_unsupported"
        assert failed.actions == ()
        assert runtime.deleted == ["action-limit"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_action_expiry_hides_actions_before_cleanup_converges() -> None:
    application = AdkApplicationSpec(
        "action_expiry",
        "v1",
        "graph",
        _factory,
        action_specs=(
            WorkflowActionSpec("confirm", "confirmation", {"type": "boolean"}),
        ),
        resumable=True,
    )
    runtime = BlockingDeleteRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction("runtime-action", "confirm", "confirmation"),
                ),
                resume_token="resume-token",  # security-test-fixture
            ),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(action_timeout_seconds=0.01, poll_interval_seconds=0.005),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "action_expiry",
            AgentRunCreateRequest(input="start", session_id="action-expiry"),
            _request(),
            idempotency_key="action-expiry",
        )
        waiting = await _terminal(service, created.run_id)
        assert waiting.status is AgentRunStatus.REQUIRES_ACTION
        await runtime.delete_entered.wait()

        canceling = await service.get_run(created.run_id, _request())
        assert canceling.status is AgentRunStatus.CANCELING
        assert canceling.actions == ()
        assert canceling.error is not None
        assert canceling.error.code == "agent_run_expired"

        runtime.release_delete.set()
        expired = await _terminal(service, created.run_id)
        assert expired.status is AgentRunStatus.EXPIRED
        assert expired.error is not None
        assert expired.error.code == "agent_run_expired"
    finally:
        runtime.release_delete.set()
        await service.close()


@pytest.mark.asyncio
async def test_cleanup_retry_preserves_the_original_terminal_error() -> None:
    application = AdkApplicationSpec("cleanup_error", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="late"),)
    )
    runtime.delay_seconds = 0.1
    runtime.fail_delete = True
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(
            phase_timeout_seconds=0.03,
            overall_lifetime_seconds=0.03,
            action_timeout_seconds=0.01,
            poll_interval_seconds=0.005,
        ),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "cleanup_error",
            AgentRunCreateRequest(input="start", session_id="cleanup-error"),
            _request(),
            idempotency_key="cleanup-error",
        )
        await _wait_until_deleted(runtime, "cleanup-error")
        cleanup_failed = await service.get_run(created.run_id, _request())
        assert cleanup_failed.status is AgentRunStatus.CANCELING
        assert cleanup_failed.error is not None
        assert cleanup_failed.error.code == "agent_run_expired"

        runtime.fail_delete = False
        expired = await _terminal(service, created.run_id)
        assert expired.status is AgentRunStatus.EXPIRED
        assert expired.error is not None
        assert expired.error.code == "agent_run_expired"
    finally:
        runtime.fail_delete = False
        await service.close()


@pytest.mark.asyncio
async def test_resume_idempotency_replays_and_rejects_changed_payload() -> None:
    application = AdkApplicationSpec(
        "resume_idempotency",
        "v1",
        "team",
        _factory,
        action_specs=(
            WorkflowActionSpec("confirm", "confirmation", {"type": "boolean"}),
        ),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction("runtime-action", "confirm", "confirmation"),
                ),
                resume_token="resume-token",  # security-test-fixture
            ),
            WorkflowInvocationResult(state="succeeded", output="complete"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "resume_idempotency",
            AgentRunCreateRequest(input="start", session_id="resume-idempotency"),
            _request(),
            idempotency_key="resume-create",
        )
        pending = await _terminal(service, created.run_id)
        request = AgentRunResumeRequest(
            version=pending.version,
            actions=[
                AgentRunConfirmationResponse(
                    type="confirmation",
                    action_id=pending.actions[0].action_id,
                    confirmed=True,
                )
            ],
        )
        first = await service.resume_run(
            created.run_id,
            request,
            _request(),
            idempotency_key="resume-key",
        )
        replay = await service.resume_run(
            created.run_id,
            request,
            _request(),
            idempotency_key="resume-key",
        )
        assert replay.run_id == first.run_id

        changed = request.model_copy(deep=True)
        changed.actions[0].confirmed = False
        with pytest.raises(AgentRunConflictError) as conflict:
            await service.resume_run(
                created.run_id,
                changed,
                _request(),
                idempotency_key="resume-key",
            )
        assert conflict.value.code == "idempotency_conflict"
        assert (
            await _terminal(service, created.run_id)
        ).status is AgentRunStatus.SUCCEEDED
        assert len(runtime.invocations) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_multiple_actions_must_be_resumed_atomically() -> None:
    application = AdkApplicationSpec(
        "multi_action",
        "v1",
        "team",
        _factory,
        action_specs=(
            WorkflowActionSpec("details", "input", {"type": "string"}),
            WorkflowActionSpec("confirm", "confirmation", {"type": "boolean"}),
        ),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction("runtime-input", "details", "input"),
                    WorkflowPendingAction("runtime-confirm", "confirm", "confirmation"),
                ),
                resume_token="resume-token",  # security-test-fixture
            ),
            WorkflowInvocationResult(state="succeeded", output="complete"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "multi_action",
            AgentRunCreateRequest(input="start", session_id="multi-action"),
            _request(),
            idempotency_key="multi-create",
        )
        pending = await _terminal(service, created.run_id)
        with pytest.raises(AgentRunConflictError) as incomplete:
            await service.resume_run(
                created.run_id,
                AgentRunResumeRequest(
                    version=pending.version,
                    actions=[
                        AgentRunInputResponse(
                            type="input",
                            action_id=pending.actions[0].action_id,
                            value="ready",
                        )
                    ],
                ),
                _request(),
                idempotency_key="multi-incomplete",
            )
        assert incomplete.value.code == "agent_run_resume_conflict"

        await service.resume_run(
            created.run_id,
            AgentRunResumeRequest(
                version=pending.version,
                actions=[
                    AgentRunInputResponse(
                        type="input",
                        action_id=pending.actions[0].action_id,
                        value="ready",
                    ),
                    AgentRunConfirmationResponse(
                        type="confirmation",
                        action_id=pending.actions[1].action_id,
                        confirmed=True,
                    ),
                ],
            ),
            _request(),
            idempotency_key="multi-complete",
        )
        completed = await _terminal(service, created.run_id)
        assert completed.status is AgentRunStatus.SUCCEEDED
        assert [
            response.runtime_action_id for response in runtime.invocations[1].responses
        ] == [
            "runtime-input",
            "runtime-confirm",
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_tenants_isolate_identical_agent_session_ids() -> None:
    application = AdkApplicationSpec("isolated", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(state="succeeded", output="tenant-a"),
            WorkflowInvocationResult(state="succeeded", output="tenant-b"),
        )
    )
    resolver = MutableResolver()
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=resolver,
    )
    await service.start()
    try:
        first = await service.create_run(
            "isolated",
            AgentRunCreateRequest(input="first", session_id="shared"),
            _request(),
            idempotency_key="tenant-a",
        )
        await _terminal(service, first.run_id)

        resolver.principal_id = "tenant-b"
        second = await service.create_run(
            "isolated",
            AgentRunCreateRequest(input="second", session_id="shared"),
            _request(),
            idempotency_key="tenant-b",
        )
        assert (await _terminal(service, second.run_id)).output == "tenant-b"
        with pytest.raises(AgentRunNotFoundError):
            await service.get_run(first.run_id, _request())

        resolver.principal_id = "tenant-a"
        assert (await service.get_run(first.run_id, _request())).output == "tenant-a"
        assert [identity.principal_id for identity in runtime.identities] == [
            "tenant-a",
            "tenant-b",
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_definition_fingerprint_change_rejects_old_continuation() -> None:
    original = AdkApplicationSpec("revision_scope", "v1", "graph", _factory)
    changed = AdkApplicationSpec(
        "revision_scope", "v1", "graph", _factory, output_format="json"
    )
    assert original.fingerprint != changed.fingerprint
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="complete"),)
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((original,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "revision_scope",
            AgentRunCreateRequest(input="start", session_id="revision-session"),
            _request(),
            idempotency_key="revision-create",
        )
        await _terminal(service, created.run_id)
        service._registry = AdkApplicationRegistry((changed,))

        with pytest.raises(AgentRunNotFoundError):
            await service.create_run(
                "revision_scope",
                AgentRunCreateRequest(
                    input="continue",
                    session_action="continue",
                    session_id="revision-session",
                ),
                _request(),
                idempotency_key="revision-continue",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_process_restart_cannot_continue_an_in_memory_session() -> None:
    application = AdkApplicationSpec("restart_scope", "v1", "graph", _factory)
    registry = AdkApplicationRegistry((application,))
    first_runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="complete"),)
    )
    first_service = _service(
        first_runtime,
        registry,
        _config(),
        identity_resolver=FixedResolver(),
    )
    await first_service.start()
    created = await first_service.create_run(
        "restart_scope",
        AgentRunCreateRequest(input="start", session_id="restart-session"),
        _request(),
        idempotency_key="restart-create",
    )
    await _terminal(first_service, created.run_id)
    await first_service.close()

    restarted = _service(
        FakeWorkflowRuntime(()),
        registry,
        _config(),
        identity_resolver=FixedResolver(),
    )
    await restarted.start()
    try:
        with pytest.raises(AgentRunNotFoundError):
            await restarted.create_run(
                "restart_scope",
                AgentRunCreateRequest(
                    input="continue",
                    session_action="continue",
                    session_id="restart-session",
                ),
                _request(),
                idempotency_key="restart-continue",
            )
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_http_create_returns_before_disconnected_run_finishes() -> None:
    application = AdkApplicationSpec("transport", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="complete"),)
    )
    runtime.block = asyncio.Event()
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        router = create_agent_runs_router(service)
        create_endpoint = _route_endpoint(
            router, "/api/v1/agents/{agent_id}/runs", "POST"
        )
        response = await create_endpoint(
            "transport",
            AgentRunCreateRequest(input="start", session_id="transport-session"),
            Request(
                {"type": "http", "method": "POST", "path": "/"},
                receive=lambda: {"type": "http.disconnect"},
            ),
            idempotency_key="transport-create",
        )
        payload = json.loads(response.body)
        run_id = payload["data"]["run_id"]
        assert response.status_code == 202
        assert response.headers["Location"] == f"/api/v1/agent-runs/{run_id}"
        assert response.headers["Cache-Control"] == "no-store"

        runtime.block.set()
        completed = await _terminal(service, run_id)
        assert completed.status is AgentRunStatus.SUCCEEDED
        assert completed.output == "complete"
    finally:
        if runtime.block is not None:
            runtime.block.set()
        await service.close()


@pytest.mark.asyncio
async def test_http_resume_returns_async_polling_headers() -> None:
    application = AdkApplicationSpec(
        "resume_transport",
        "v1",
        "team",
        _factory,
        action_specs=(
            WorkflowActionSpec("confirm", "confirmation", {"type": "boolean"}),
        ),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction(
                        "private-runtime-action", "confirm", "confirmation"
                    ),
                ),
                resume_token="private-resume-token",  # security-test-fixture
            ),
            WorkflowInvocationResult(state="succeeded", output="complete"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        router = create_agent_runs_router(service)
        create_endpoint = _route_endpoint(
            router, "/api/v1/agents/{agent_id}/runs", "POST"
        )
        get_endpoint = _route_endpoint(router, "/api/v1/agent-runs/{run_id}", "GET")
        resume_endpoint = _route_endpoint(
            router, "/api/v1/agent-runs/{run_id}/resume", "POST"
        )
        created_response = await create_endpoint(
            "resume_transport",
            AgentRunCreateRequest(input="start", session_id="resume-transport"),
            _request(),
            idempotency_key="resume-transport-create",
        )
        run_id = json.loads(created_response.body)["data"]["run_id"]
        pending = await _terminal(service, run_id)

        polling = await get_endpoint(run_id, _request())
        assert polling.headers["Retry-After"] == "1"
        assert "private-runtime-action" not in polling.body.decode()
        assert "private-resume-token" not in polling.body.decode()

        resumed = await resume_endpoint(
            run_id,
            AgentRunResumeRequest(
                version=pending.version,
                actions=[
                    AgentRunConfirmationResponse(
                        type="confirmation",
                        action_id=pending.actions[0].action_id,
                        confirmed=True,
                    )
                ],
            ),
            _request(),
            idempotency_key="resume-transport-resume",
        )
        assert resumed.status_code == 202
        assert resumed.headers["Location"] == f"/api/v1/agent-runs/{run_id}"
        assert resumed.headers["Cache-Control"] == "no-store"

        await _terminal(service, run_id)
        terminal = await get_endpoint(run_id, _request())
        assert "Retry-After" not in terminal.headers
        assert terminal.headers["Cache-Control"] == "no-store"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_wins_before_success_is_applied() -> None:
    application = AdkApplicationSpec("cancel_success", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="discarded"),)
    )
    service = PausingApplyService(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
        authorizer=_allow_application,
    )
    await service.start()
    try:
        created = await service.create_run(
            "cancel_success",
            AgentRunCreateRequest(input="start", session_id="cancel-success"),
            _request(),
            idempotency_key="cancel-success",
        )
        await service.apply_entered.wait()
        await service.cancel_run(created.run_id, _request())
        service.release_apply.set()

        canceled = await _terminal(service, created.run_id)
        assert canceled.status is AgentRunStatus.CANCELED
        assert canceled.output is None
        assert runtime.deleted == ["cancel-success"]
    finally:
        service.release_apply.set()
        await service.close()


@pytest.mark.asyncio
async def test_missing_authorization_dependencies_fail_closed() -> None:
    application = AdkApplicationSpec("closed", "v1", "graph", _factory)
    registry = AdkApplicationRegistry((application,))

    missing_authorizer = AppAgentRunsService(
        FakeWorkflowRuntime(()),
        registry,
        _config(),
        identity_resolver=FixedResolver(),
    )
    await missing_authorizer.start()
    try:
        with pytest.raises(AgentRunNotFoundError):
            await missing_authorizer.create_run(
                "closed",
                AgentRunCreateRequest(input="not admitted"),
                _request(),
                idempotency_key="missing-authorizer",
            )
    finally:
        await missing_authorizer.close()

    missing_identity = AppAgentRunsService(
        FakeWorkflowRuntime(()),
        registry,
        _config(),
        authorizer=_allow_application,
    )
    await missing_identity.start()
    try:
        with pytest.raises(AuthNotConfiguredError):
            await missing_identity.create_run(
                "closed",
                AgentRunCreateRequest(input="not authenticated"),
                _request(),
                idempotency_key="missing-identity",
            )
    finally:
        await missing_identity.close()


@pytest.mark.asyncio
async def test_phase_deadline_wins_when_runtime_swallows_cancellation() -> None:
    application = AdkApplicationSpec("hard_deadline", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="late success"),)
    )
    runtime.block = asyncio.Event()
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(
            phase_timeout_seconds=0.02,
            overall_lifetime_seconds=0.2,
            action_timeout_seconds=0.1,
        ),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "hard_deadline",
            AgentRunCreateRequest(input="start", session_id="hard-deadline"),
            _request(),
            idempotency_key="hard-deadline",
        )
        await asyncio.wait_for(runtime.cancel_seen.wait(), timeout=0.2)

        timed_out = await service.get_run(created.run_id, _request())
        assert timed_out.status is AgentRunStatus.CANCELING
        assert timed_out.error is not None
        assert timed_out.error.code == "agent_phase_timeout"
        assert timed_out.output is None
        assert runtime.timeout_seconds == [pytest.approx(0.02, rel=0.2)]
        assert service.runtime_stats.active_phases == 1

        runtime.block.set()
        failed = await _terminal(service, created.run_id)
        assert failed.status is AgentRunStatus.FAILED
        assert failed.output is None
        assert runtime.deleted == ["hard-deadline"]
    finally:
        runtime.block.set()
        await service.close()


@pytest.mark.asyncio
async def test_quiesce_rejects_create_but_allows_resume_and_drain() -> None:
    application = AdkApplicationSpec(
        "draining",
        "v1",
        "team",
        _factory,
        action_specs=(
            WorkflowActionSpec("confirm", "confirmation", {"type": "boolean"}),
        ),
        resumable=True,
    )
    runtime = FakeWorkflowRuntime(
        (
            WorkflowInvocationResult(
                state="requires_action",
                actions=(
                    WorkflowPendingAction("runtime-action", "confirm", "confirmation"),
                ),
                resume_token="resume-token",  # security-test-fixture
            ),
            WorkflowInvocationResult(state="succeeded", output="complete"),
        )
    )
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    try:
        created = await service.create_run(
            "draining",
            AgentRunCreateRequest(input="start", session_id="draining-session"),
            _request(),
            idempotency_key="draining-create",
        )
        pending = await _terminal(service, created.run_id)
        await service.quiesce()

        stats = service.runtime_stats
        assert stats.accepting is False
        assert stats.draining is True
        assert stats.healthy is False
        assert stats.waiting_actions == 1
        assert stats.runs_by_status["requires_action"] == 1
        assert (await service.get_run(created.run_id, _request())).actions
        with pytest.raises(AgentRunsUnavailableError):
            await service.create_run(
                "draining",
                AgentRunCreateRequest(input="new"),
                _request(),
                idempotency_key="draining-rejected",
            )

        await service.resume_run(
            created.run_id,
            AgentRunResumeRequest(
                version=pending.version,
                actions=[
                    AgentRunConfirmationResponse(
                        type="confirmation",
                        action_id=pending.actions[0].action_id,
                        confirmed=True,
                    )
                ],
            ),
            _request(),
            idempotency_key="draining-resume",
        )
        assert (
            await _terminal(service, created.run_id)
        ).status is AgentRunStatus.SUCCEEDED
        assert await service.drain(timeout_seconds=0.1) is True
        deleted = await service.delete_session(
            "draining", "draining-session", _request()
        )
        assert deleted.deleted is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_close_drains_then_forces_cancel_and_remains_observable() -> None:
    application = AdkApplicationSpec("shutdown", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="discarded"),)
    )
    runtime.block = asyncio.Event()
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(
            phase_timeout_seconds=0.2,
            overall_lifetime_seconds=1.0,
            action_timeout_seconds=0.01,
        ),
        identity_resolver=FixedResolver(),
    )
    await service.start()
    created = await service.create_run(
        "shutdown",
        AgentRunCreateRequest(input="start", session_id="shutdown-session"),
        _request(),
        idempotency_key="shutdown-create",
    )
    try:
        for _ in range(100):
            if (
                await service.get_run(created.run_id, _request())
            ).status is AgentRunStatus.RUNNING:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("run did not start")

        close_task = asyncio.create_task(service.close())
        await asyncio.wait_for(runtime.cancel_seen.wait(), timeout=0.2)
        during_close = await service.get_run(created.run_id, _request())
        assert during_close.status is AgentRunStatus.CANCELING
        assert service.runtime_stats.draining is True
        with pytest.raises(AgentRunsUnavailableError):
            await service.create_run(
                "shutdown",
                AgentRunCreateRequest(input="rejected"),
                _request(),
                idempotency_key="shutdown-rejected",
            )

        runtime.block.set()
        await close_task
        closed = await service.get_run(created.run_id, _request())
        assert closed.status is AgentRunStatus.CANCELED
        assert closed.output is None
        assert service.runtime_stats.draining is False
    finally:
        runtime.block.set()
        if service.runtime_stats.draining:
            await service.close()


@pytest.mark.asyncio
async def test_observer_receives_only_low_cardinality_runtime_events() -> None:
    application = AdkApplicationSpec("observed", "v1", "graph", _factory)
    runtime = FakeWorkflowRuntime(
        (WorkflowInvocationResult(state="succeeded", output="complete"),)
    )
    runtime.block = asyncio.Event()
    observer = RecordingObserver()
    service = _service(
        runtime,
        AdkApplicationRegistry((application,)),
        _config(max_entries=1, max_concurrent_phases=1),
        identity_resolver=FixedResolver(),
        observer=observer,
    )
    await service.start()
    try:
        created = await service.create_run(
            "observed",
            AgentRunCreateRequest(input="start", session_id="observed-session"),
            _request(),
            idempotency_key="observed-create",
        )
        for _ in range(100):
            if (
                await service.get_run(created.run_id, _request())
            ).status is AgentRunStatus.RUNNING:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("observed run did not start")

        with pytest.raises(AgentRunsCapacityError):
            await service.create_run(
                "observed",
                AgentRunCreateRequest(input="rejected"),
                _request(),
                idempotency_key="observed-capacity",
            )
        runtime.block.set()
        assert (
            await _terminal(service, created.run_id)
        ).status is AgentRunStatus.SUCCEEDED
        await asyncio.sleep(0)

        assert observer.transitions == ["queued", "running", "succeeded"]
        assert observer.terminals == ["succeeded"]
        assert len(observer.phases) == 1
        assert observer.phases[0][0] == "succeeded"
        assert observer.phases[0][1] >= 0
        assert observer.capacity_rejections == 1
        assert observer.runtime[-1]["runs_by_status"] == {
            "queued": 0,
            "running": 0,
            "requires_action": 0,
            "canceling": 0,
            "succeeded": 1,
            "failed": 0,
            "canceled": 0,
            "expired": 0,
        }
    finally:
        runtime.block.set()
        await service.close()


@pytest.mark.asyncio
async def test_observer_failure_degrades_health_without_failing_runs() -> None:
    application = AdkApplicationSpec("observer_failure", "v1", "graph", _factory)
    observer = RecordingObserver(fail_runtime=True)
    service = _service(
        FakeWorkflowRuntime(
            (WorkflowInvocationResult(state="succeeded", output="complete"),)
        ),
        AdkApplicationRegistry((application,)),
        _config(),
        identity_resolver=FixedResolver(),
        observer=observer,
    )
    await service.start()
    try:
        await asyncio.sleep(0)
        assert service.healthy is False

        created = await service.create_run(
            "observer_failure",
            AgentRunCreateRequest(input="start"),
            _request(),
            idempotency_key="observer-failure",
        )
        completed = await _terminal(service, created.run_id)
        assert completed.status is AgentRunStatus.SUCCEEDED
        assert completed.output == "complete"
        assert service.runtime_stats.healthy is False
    finally:
        await service.close()
