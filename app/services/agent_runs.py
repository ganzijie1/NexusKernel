"""Authenticated, process-local orchestration for asynchronous Agent runs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, cast

from fastapi import Request
from jsonschema import (  # type: ignore[import-untyped]
    ValidationError as JsonSchemaValidationError,
)
from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import JsonValue

from app.modules.agent_runs.api import (
    AgentRunAction,
    AgentRunCreateRequest,
    AgentRunInputResponse,
    AgentRunResumeRequest,
    AgentRunSnapshot,
    AgentRunStatus,
    AgentSessionDeleteResult,
)
from app.modules.agent_runs.api.schemas import AgentRunError, TERMINAL_RUN_STATUSES
from app.modules.agent_runs.module import AgentRunsModuleConfig
from app.services.identity import (
    BearerIdentityResolver,
    TrustedIdentityResolver,
)
from nexus_kernel._validation import require_positive_finite_number
from nexus_kernel.ai import (
    AdkApplicationRegistry,
    AdkApplicationSpec,
    AgentIdentity,
    AgentToolSpec,
    TokenUsage,
    WorkflowActionResponse,
    WorkflowInvocation,
    WorkflowInvocationResult,
    WorkflowRuntime,
)
from nexus_kernel.ai.contracts import thaw_json
from nexus_kernel.ai.errors import AiTimeoutError, AiWorkflowActionUnsupportedError
from nexus_kernel.errors import FrameworkError


class AgentRunsError(FrameworkError):
    """Base class for stable Agent Runs failures."""

    code = "agent_runs_unavailable"
    status_code = 503
    public_message = "Agent Runs is unavailable."

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        public_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or type(self).code
        self.status_code = status_code or type(self).status_code
        self.public_message = public_message or type(self).public_message


class AgentRunNotFoundError(AgentRunsError):
    code = "agent_run_not_found"
    status_code = 404
    public_message = "Agent resource was not found."


class AgentRunConflictError(AgentRunsError):
    code = "agent_run_conflict"
    status_code = 409
    public_message = "Agent run state conflicts with the request."


class AgentRunsCapacityError(AgentRunsError):
    code = "agent_run_capacity_exceeded"
    status_code = 429
    public_message = "Agent run capacity is exhausted."


class AgentRunsUnavailableError(AgentRunsError):
    code = "agent_runs_unavailable"
    status_code = 503
    public_message = "Agent Runs is unavailable."


class AgentRunPayloadError(AgentRunsError):
    code = "agent_payload_too_large"
    status_code = 413
    public_message = "Agent run payload is too large."


class AgentRunsShutdownError(AgentRunsError):
    pass


AgentAuthorizer = Callable[[AgentIdentity, AdkApplicationSpec], bool | Awaitable[bool]]


class AgentRunsObserver(Protocol):
    """Low-cardinality process metrics consumed outside the service lock."""

    def set_agent_runs_runtime(
        self,
        *,
        queue_depth: int,
        active_phases: int,
        waiting_actions: int,
        cleanup_debt: int,
        runs_by_status: Mapping[str, int],
    ) -> None: ...

    def record_agent_run_transition(self, status: str) -> None: ...

    def record_agent_run_terminal(self, status: str) -> None: ...

    def record_agent_run_phase(self, outcome: str, duration_seconds: float) -> None: ...

    def record_agent_runs_capacity_rejection(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentRunsRuntimeStats:
    """Identifier-free in-process state suitable for health and metrics."""

    accepting: bool
    draining: bool
    healthy: bool
    queue_depth: int
    active_phases: int
    waiting_actions: int
    cleanup_debt: int
    runs_by_status: Mapping[str, int]


class AgentRunsService(Protocol):
    @property
    def poll_interval_seconds(self) -> float: ...

    @property
    def runtime_stats(self) -> AgentRunsRuntimeStats: ...

    async def quiesce(self) -> None: ...

    async def drain(self, *, timeout_seconds: float | None = None) -> bool: ...

    async def create_run(
        self,
        agent_id: str,
        request: AgentRunCreateRequest,
        http_request: Request,
        *,
        idempotency_key: str,
    ) -> AgentRunSnapshot: ...

    async def get_run(self, run_id: str, http_request: Request) -> AgentRunSnapshot: ...

    async def resume_run(
        self,
        run_id: str,
        request: AgentRunResumeRequest,
        http_request: Request,
        *,
        idempotency_key: str,
    ) -> AgentRunSnapshot: ...

    async def cancel_run(
        self, run_id: str, http_request: Request
    ) -> AgentRunSnapshot: ...

    async def delete_session(
        self, agent_id: str, session_id: str, http_request: Request
    ) -> AgentSessionDeleteResult: ...


@dataclass(frozen=True, slots=True)
class _SessionKey:
    principal_id: str
    agent_id: str
    fingerprint: str
    session_id: str


@dataclass(slots=True)
class _Session:
    application: AdkApplicationSpec
    identity: AgentIdentity
    busy_run_id: str | None
    last_touched_mono: float
    last_run_id: str | None


@dataclass(frozen=True, slots=True)
class _PendingAction:
    public_id: str
    runtime_id: str
    code: str
    kind: Literal["input", "confirmation"]
    expires_at: datetime
    expires_mono: float


@dataclass(frozen=True, slots=True)
class _QueuedPhase:
    run_id: str
    invocation: WorkflowInvocation
    kind: Literal["initial", "resume"]


@dataclass(frozen=True, slots=True)
class _PhaseExecution:
    result: WorkflowInvocationResult | None = None
    error_code: str | None = None
    error_terminal: AgentRunStatus = AgentRunStatus.FAILED
    timed_out: bool = False


@dataclass(slots=True)
class _RunRecord:
    run_id: str
    application: AdkApplicationSpec
    identity: AgentIdentity
    session_key: _SessionKey
    session_was_new: bool
    status: AgentRunStatus
    version: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    created_mono: float
    expires_mono: float
    terminal_mono: float | None = None
    output: object | None = None
    usage: TokenUsage | None = None
    model: str | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    pending_actions: tuple[_PendingAction, ...] = ()
    resume_token: str | None = None
    resume_count: int = 0
    phase_task: asyncio.Task[WorkflowInvocationResult] | None = None
    terminal_after_cleanup: AgentRunStatus | None = None
    cleanup_failed: bool = False
    session_touched: bool = False


@dataclass(frozen=True, slots=True)
class _IdempotencyRecord:
    digest: bytes
    run_id: str


class AppAgentRunsService:
    """Bounded FIFO execution and retention for process-local Agent runs."""

    def __init__(
        self,
        runtime: WorkflowRuntime,
        registry: AdkApplicationRegistry,
        config: AgentRunsModuleConfig,
        *,
        service_token: str | None = None,
        identity_resolver: TrustedIdentityResolver | None = None,
        authorizer: AgentAuthorizer | None = None,
        tools: Sequence[AgentToolSpec] = (),
        session_ttl_seconds: float = 1800.0,
        observer: AgentRunsObserver | None = None,
    ) -> None:
        if service_token is not None and identity_resolver is not None:
            raise ValueError("configure service_token or identity_resolver, not both")
        names = [tool.name for tool in tools]
        if len(set(names)) != len(names):
            raise ValueError("Agent tool names must be unique")
        require_positive_finite_number(session_ttl_seconds, "session_ttl_seconds")
        self._runtime = runtime
        self._registry = registry
        self._config = config
        self._identity_resolver = identity_resolver or BearerIdentityResolver(
            service_token
        )
        self._authorizer = authorizer
        self._tools = {tool.name: tool for tool in tools}
        self._observer = observer
        self._session_ttl_seconds = session_ttl_seconds
        self._runs: dict[str, _RunRecord] = {}
        self._sessions: dict[_SessionKey, _Session] = {}
        self._idempotency: dict[tuple[str, str, str], _IdempotencyRecord] = {}
        self._phases: asyncio.Queue[_QueuedPhase] = asyncio.Queue(
            maxsize=config.max_entries
        )
        self._lock = asyncio.Lock()
        self._session_admission_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._workers: tuple[asyncio.Task[None], ...] = ()
        self._sweeper: asyncio.Task[None] | None = None
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._run_cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_cleanup_keys: set[_SessionKey] = set()
        self._close_task: asyncio.Task[bool] | None = None
        self._started = False
        self._accepting = False
        self._cleanup_failed = False
        self._observer_failed = False
        self._stopping = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._draining = False
        self._digest_key = secrets.token_bytes(32)

    @property
    def poll_interval_seconds(self) -> float:
        return self._config.poll_interval_seconds

    @property
    def runtime_stats(self) -> AgentRunsRuntimeStats:
        runs_by_status = {
            status.value: sum(record.status is status for record in self._runs.values())
            for status in AgentRunStatus
        }
        active_phases = sum(
            record.phase_task is not None and not record.phase_task.done()
            for record in self._runs.values()
        )
        cleanup_debt = sum(not task.done() for task in self._cleanup_tasks)
        cleanup_debt += sum(record.cleanup_failed for record in self._runs.values())
        cleanup_debt += sum(
            session.busy_run_id == "cleanup_failed"
            for session in self._sessions.values()
        )
        return AgentRunsRuntimeStats(
            accepting=self._accepting,
            draining=self._draining,
            healthy=self.healthy,
            queue_depth=self._phases.qsize(),
            active_phases=active_phases,
            waiting_actions=sum(
                len(record.pending_actions)
                for record in self._runs.values()
                if record.status is AgentRunStatus.REQUIRES_ACTION
            ),
            cleanup_debt=cleanup_debt,
            runs_by_status=MappingProxyType(runs_by_status),
        )

    @property
    def healthy(self) -> bool:
        if (
            not self._started
            or not self._accepting
            or self._cleanup_failed
            or self._observer_failed
            or self._stopping.is_set()
            or self._draining
        ):
            return False
        if len(self._workers) != self._config.max_concurrent_phases or any(
            worker.done() for worker in self._workers
        ):
            return False
        return self._sweeper is not None and not self._sweeper.done()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if self._stopping.is_set():
                raise AgentRunsUnavailableError("Agent Runs service is stopping")
            self._workers = tuple(
                asyncio.create_task(
                    self._worker_loop(), name=f"agent_runs_phase_{index}"
                )
                for index in range(self._config.max_concurrent_phases)
            )
            for worker in self._workers:
                worker.add_done_callback(self._consume_background_result)
            self._sweeper = asyncio.create_task(
                self._sweeper_loop(), name="agent_runs_sweeper"
            )
            self._sweeper.add_done_callback(self._consume_background_result)
            self._started = True
            self._accepting = True
            self._draining = False
            self._schedule_observation("runtime")

    async def quiesce(self) -> None:
        """Stop admitting new runs while existing runs remain operable."""

        async with self._lock:
            if not self._started:
                return
            self._accepting = False
            self._draining = True
            self._schedule_observation("runtime")

    async def drain(self, *, timeout_seconds: float | None = None) -> bool:
        """Quiesce and wait for every accepted run to become terminal."""

        timeout = (
            self._config.action_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        require_positive_finite_number(timeout, "drain timeout")
        await self.quiesce()
        if self._drained.is_set():
            return True
        try:
            async with asyncio.timeout(float(timeout)):
                await self._drained.wait()
        except TimeoutError:
            return self._drained.is_set()
        return True

    def _schedule_observation(
        self,
        kind: Literal["runtime", "transition", "terminal", "phase", "capacity"],
        value: str | None = None,
        duration_seconds: float = 0.0,
    ) -> None:
        if self._observer is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon(self._deliver_observation, kind, value, duration_seconds)

    def _deliver_observation(
        self,
        kind: Literal["runtime", "transition", "terminal", "phase", "capacity"],
        value: str | None,
        duration_seconds: float,
    ) -> None:
        observer = self._observer
        if observer is None:
            return
        try:
            if kind == "transition" and value is not None:
                observer.record_agent_run_transition(value)
            elif kind == "terminal" and value is not None:
                observer.record_agent_run_terminal(value)
            elif kind == "phase" and value is not None:
                observer.record_agent_run_phase(value, duration_seconds)
            elif kind == "capacity":
                observer.record_agent_runs_capacity_rejection()
            stats = self.runtime_stats
            observer.set_agent_runs_runtime(
                queue_depth=stats.queue_depth,
                active_phases=stats.active_phases,
                waiting_actions=stats.waiting_actions,
                cleanup_debt=stats.cleanup_debt,
                runs_by_status=stats.runs_by_status,
            )
        except Exception:
            self._observer_failed = True

    def _consume_background_result(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not self._stopping.is_set():
            self._cleanup_failed = True

    async def close(self) -> None:
        async with self._lifecycle_lock:
            task = self._close_task
            if task is None:
                task = asyncio.create_task(
                    self._close_once(), name="agent_runs_shutdown"
                )
                self._close_task = task
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                complete = await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.done():
                    if task.cancelled():
                        raise
                    complete = task.result()
                    cancelled = cancelled or exc
                    break
                cancelled = cancelled or exc
        if not complete:
            async with self._lifecycle_lock:
                if self._close_task is task:
                    self._close_task = None
            if cancelled is not None:
                raise cancelled
            raise AgentRunsShutdownError("Agent Runs shutdown has not converged")
        if cancelled is not None:
            raise cancelled

    async def create_run(
        self,
        agent_id: str,
        request: AgentRunCreateRequest,
        http_request: Request,
        *,
        idempotency_key: str,
    ) -> AgentRunSnapshot:
        identity = await self._identity_resolver.resolve(http_request)
        application = await self._resolve_application(agent_id, identity)
        self._validate_idempotency_key(idempotency_key)
        self._validate_payload(request.input)
        digest = self._digest(
            {
                "agent_id": agent_id,
                "input": request.input,
                "session_action": request.session_action,
                "session_id": request.session_id,
            }
        )
        scope = (identity.principal_id, f"create:{agent_id}", idempotency_key)
        session_id = request.session_id or f"s_{secrets.token_hex(16)}"
        session_key = _SessionKey(
            identity.principal_id,
            application.agent_id,
            application.fingerprint,
            session_id,
        )
        session_was_new = request.session_action == "new"
        async with self._lock:
            self._ensure_accepting()
            self._prune_terminal_locked(time.monotonic())
            replay = self._idempotency.get(scope)
            if replay is not None:
                return self._idempotent_snapshot_locked(replay, digest)
            if len(self._runs) >= self._config.max_entries:
                raise self._capacity_error("Agent run capacity is exhausted")

        async with self._session_admission_lock:
            async with self._lock:
                self._ensure_accepting()
                self._prune_terminal_locked(time.monotonic())
                replay = self._idempotency.get(scope)
                if replay is not None:
                    return self._idempotent_snapshot_locked(replay, digest)
                if len(self._runs) >= self._config.max_entries:
                    raise self._capacity_error("Agent run capacity is exhausted")
            await self._prepare_session_for_run(
                session_key,
                session_was_new=session_was_new,
            )
            now = datetime.now(UTC)
            now_mono = time.monotonic()
            async with self._lock:
                self._ensure_accepting()
                self._prune_terminal_locked(now_mono)
                replay = self._idempotency.get(scope)
                if replay is not None:
                    return self._idempotent_snapshot_locked(replay, digest)
                if len(self._runs) >= self._config.max_entries:
                    raise self._capacity_error("Agent run capacity is exhausted")
                existing_session = self._sessions.get(session_key)
                if session_was_new:
                    if existing_session is not None:
                        raise AgentRunConflictError(
                            "Session already exists", code="agent_session_conflict"
                        )
                elif existing_session is None:
                    raise AgentRunNotFoundError("Agent session was not found")
                elif existing_session.busy_run_id is not None:
                    raise AgentRunConflictError(
                        "Agent session is busy", code="agent_session_busy"
                    )
                run_id = f"run_{secrets.token_hex(16)}"
                expires_at = now + timedelta(
                    seconds=self._config.overall_lifetime_seconds
                )
                record = _RunRecord(
                    run_id=run_id,
                    application=application,
                    identity=identity,
                    session_key=session_key,
                    session_was_new=session_was_new,
                    status=AgentRunStatus.QUEUED,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                    created_mono=now_mono,
                    expires_mono=(now_mono + self._config.overall_lifetime_seconds),
                )
                phase = _QueuedPhase(
                    run_id,
                    WorkflowInvocation(session_id=session_id, text=request.input),
                    "initial",
                )
                try:
                    self._phases.put_nowait(phase)
                except asyncio.QueueFull:
                    raise self._capacity_error("Agent run queue is full") from None
                self._runs[run_id] = record
                self._drained.clear()
                self._schedule_observation("transition", AgentRunStatus.QUEUED.value)
                if existing_session is None:
                    self._sessions[session_key] = _Session(
                        application=application,
                        identity=identity,
                        busy_run_id=run_id,
                        last_touched_mono=now_mono,
                        last_run_id=run_id,
                    )
                else:
                    existing_session.busy_run_id = run_id
                    existing_session.last_run_id = run_id
                self._idempotency[scope] = _IdempotencyRecord(digest, run_id)
                return self._snapshot(record)

    async def get_run(self, run_id: str, http_request: Request) -> AgentRunSnapshot:
        identity = await self._identity_resolver.resolve(http_request)
        await self._authorize_run(run_id, identity)
        async with self._lock:
            record = self._owned_run(run_id, identity)
            return self._snapshot(record)

    async def resume_run(
        self,
        run_id: str,
        request: AgentRunResumeRequest,
        http_request: Request,
        *,
        idempotency_key: str,
    ) -> AgentRunSnapshot:
        identity = await self._identity_resolver.resolve(http_request)
        await self._authorize_run(run_id, identity)
        self._validate_idempotency_key(idempotency_key)
        payload = {
            "run_id": run_id,
            "version": request.version,
            "actions": [action.model_dump(mode="json") for action in request.actions],
        }
        self._validate_payload(payload)
        digest = self._digest(payload)
        scope = (identity.principal_id, f"resume:{run_id}", idempotency_key)
        async with self._lock:
            self._ensure_operational()
            record = self._owned_run(run_id, identity)
            replay = self._idempotency.get(scope)
            if replay is not None:
                if not hmac.compare_digest(replay.digest, digest):
                    raise AgentRunConflictError(
                        "Idempotency key payload differs",
                        code="idempotency_conflict",
                    )
                return self._snapshot(record)
            if record.status is not AgentRunStatus.REQUIRES_ACTION:
                raise AgentRunConflictError(
                    "Agent run does not require action",
                    code="agent_run_not_waiting",
                )
            if request.version != record.version:
                raise AgentRunConflictError(
                    "Agent run version changed",
                    code="agent_run_version_conflict",
                )
            if record.resume_count >= self._config.max_resumes:
                raise AgentRunConflictError(
                    "Agent run resume limit was reached",
                    code="agent_run_resume_conflict",
                )
            now_mono = time.monotonic()
            if now_mono >= record.expires_mono or any(
                now_mono >= action.expires_mono for action in record.pending_actions
            ):
                raise AgentRunConflictError(
                    "Agent run action expired", code="agent_run_resume_conflict"
                )
            provided = {action.action_id: action for action in request.actions}
            expected = {action.public_id: action for action in record.pending_actions}
            if provided.keys() != expected.keys():
                raise AgentRunConflictError(
                    "All pending actions must be replaced",
                    code="agent_run_resume_conflict",
                )
            responses: list[WorkflowActionResponse] = []
            for action_id, pending in expected.items():
                response = provided[action_id]
                if response.type != pending.kind:
                    raise AgentRunConflictError(
                        "Agent action type changed",
                        code="agent_run_resume_conflict",
                    )
                value = (
                    response.value
                    if isinstance(response, AgentRunInputResponse)
                    else response.confirmed
                )
                action_spec = next(
                    (
                        candidate
                        for candidate in record.application.action_specs
                        if candidate.code == pending.code
                    ),
                    None,
                )
                if action_spec is None or action_spec.kind != pending.kind:
                    raise AgentRunConflictError(
                        "Agent action contract changed",
                        code="agent_run_resume_conflict",
                    )
                try:
                    Draft202012Validator(
                        thaw_json(action_spec.response_schema)
                    ).validate(value)
                except JsonSchemaValidationError:
                    raise AgentRunConflictError(
                        "Agent action response is invalid",
                        code="agent_run_resume_conflict",
                    ) from None
                responses.append(
                    WorkflowActionResponse(
                        runtime_action_id=pending.runtime_id,
                        kind=pending.kind,
                        value=value,
                    )
                )
            if record.resume_token is None:
                raise AgentRunConflictError(
                    "Agent run cannot be resumed",
                    code="agent_run_resume_conflict",
                )
            phase = _QueuedPhase(
                run_id,
                WorkflowInvocation(
                    session_id=record.session_key.session_id,
                    resume_token=record.resume_token,
                    responses=tuple(responses),
                ),
                "resume",
            )
            try:
                self._phases.put_nowait(phase)
            except asyncio.QueueFull:
                raise self._capacity_error("Agent run queue is full") from None
            record.resume_count += 1
            record.pending_actions = ()
            record.resume_token = None
            self._transition(record, AgentRunStatus.QUEUED)
            self._idempotency[scope] = _IdempotencyRecord(digest, run_id)
            return self._snapshot(record)

    async def cancel_run(self, run_id: str, http_request: Request) -> AgentRunSnapshot:
        identity = await self._identity_resolver.resolve(http_request)
        await self._authorize_run(run_id, identity)
        phase_task: asyncio.Task[WorkflowInvocationResult] | None = None
        cleanup = False
        async with self._lock:
            record = self._owned_run(run_id, identity)
            if record.status in TERMINAL_RUN_STATUSES:
                return self._snapshot(record)
            if record.status is AgentRunStatus.QUEUED:
                cleanup = self._finish_queued(record, AgentRunStatus.CANCELED)
            elif record.status is not AgentRunStatus.CANCELING:
                record.terminal_after_cleanup = AgentRunStatus.CANCELED
                self._transition(record, AgentRunStatus.CANCELING)
                phase_task = record.phase_task
                if phase_task is None or phase_task.done():
                    record.phase_task = None
                    cleanup = True
            else:
                phase_task = record.phase_task
                if phase_task is None or phase_task.done():
                    record.phase_task = None
                    cleanup = True
            snapshot = self._snapshot(record)
        if phase_task is not None:
            phase_task.cancel()
        elif cleanup:
            self._enqueue_cleanup(run_id, AgentRunStatus.CANCELED)
        return snapshot

    async def delete_session(
        self, agent_id: str, session_id: str, http_request: Request
    ) -> AgentSessionDeleteResult:
        identity = await self._identity_resolver.resolve(http_request)
        application = await self._resolve_application(agent_id, identity)
        key = _SessionKey(
            identity.principal_id,
            application.agent_id,
            application.fingerprint,
            session_id,
        )
        async with self._lock:
            self._ensure_operational()
            session = self._sessions.get(key)
            if session is None:
                raise AgentRunNotFoundError("Agent session was not found")
            if session.busy_run_id is not None:
                raise AgentRunConflictError(
                    "Agent session is busy", code="agent_session_busy"
                )
            session.busy_run_id = "deleting"
        if not await self._delete_idle_session(key, session):
            raise AgentRunsUnavailableError("Agent session deletion failed")
        return AgentSessionDeleteResult(agent_id=agent_id, session_id=session_id)

    async def _resolve_application(
        self, agent_id: str, identity: AgentIdentity
    ) -> AdkApplicationSpec:
        application = self._registry.resolve(agent_id)
        if application is None:
            raise AgentRunNotFoundError("Agent application was not found")
        await self._authorize_application(identity, application)
        self._tools_for(application)
        return application

    async def _authorize_application(
        self, identity: AgentIdentity, application: AdkApplicationSpec
    ) -> None:
        authorizer = self._authorizer
        if authorizer is None:
            raise AgentRunNotFoundError("Agent application was not found")
        allowed = authorizer(identity, application)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if not allowed:
            raise AgentRunNotFoundError("Agent application was not found")

    async def _authorize_run(self, run_id: str, identity: AgentIdentity) -> None:
        async with self._lock:
            record = self._owned_run(run_id, identity)
            application = record.application
        await self._authorize_application(identity, application)

    def _idempotent_snapshot_locked(
        self, replay: _IdempotencyRecord, digest: bytes
    ) -> AgentRunSnapshot:
        if not hmac.compare_digest(replay.digest, digest):
            raise AgentRunConflictError(
                "Idempotency key payload differs", code="idempotency_conflict"
            )
        record = self._runs.get(replay.run_id)
        if record is None:
            raise AgentRunNotFoundError("Agent run was not found")
        return self._snapshot(record)

    async def _prepare_session_for_run(
        self,
        key: _SessionKey,
        *,
        session_was_new: bool,
    ) -> None:
        while True:
            now_mono = time.monotonic()
            candidate: tuple[_SessionKey, _Session] | None = None
            continuation_expired = False
            wait_for_cleanup = False
            async with self._lock:
                self._ensure_accepting()
                existing = self._sessions.get(key)
                if session_was_new:
                    if existing is not None:
                        raise AgentRunConflictError(
                            "Session already exists", code="agent_session_conflict"
                        )
                    if len(self._sessions) < self._config.max_entries:
                        return
                    idle = [
                        (candidate_key, session)
                        for candidate_key, session in self._sessions.items()
                        if session.busy_run_id is None
                    ]
                    if not idle:
                        if any(
                            session.busy_run_id == "deleting"
                            for session in self._sessions.values()
                        ):
                            wait_for_cleanup = True
                        else:
                            raise self._capacity_error(
                                "Agent session capacity is exhausted"
                            )
                    else:
                        candidate = min(
                            idle, key=lambda item: item[1].last_touched_mono
                        )
                else:
                    if existing is None:
                        raise AgentRunNotFoundError("Agent session was not found")
                    if existing.busy_run_id is not None:
                        raise AgentRunConflictError(
                            "Agent session is busy", code="agent_session_busy"
                        )
                    if (
                        now_mono - existing.last_touched_mono
                        < self._session_ttl_seconds
                    ):
                        return
                    candidate = (key, existing)
                    continuation_expired = True
                if candidate is not None:
                    candidate[1].busy_run_id = "deleting"
            if wait_for_cleanup:
                await asyncio.sleep(0.01)
                continue
            if candidate is None:
                raise AgentRunsUnavailableError("Agent session cleanup failed")
            deleted = await self._delete_idle_session(*candidate)
            if not deleted:
                raise AgentRunsUnavailableError("Agent session cleanup failed")
            if continuation_expired:
                raise AgentRunNotFoundError("Agent session was not found")

    async def _delete_idle_session(self, key: _SessionKey, session: _Session) -> bool:
        try:
            async with asyncio.timeout(self._config.phase_timeout_seconds):
                await self._runtime.delete_session(
                    session.application,
                    identity=session.identity,
                    session_id=key.session_id,
                )
        except asyncio.CancelledError:
            async with self._lock:
                current = self._sessions.get(key)
                if current is session:
                    current.busy_run_id = "cleanup_failed"
                    self._cleanup_failed = True
            raise
        except Exception:
            async with self._lock:
                current = self._sessions.get(key)
                if current is session:
                    current.busy_run_id = "cleanup_failed"
                    self._cleanup_failed = True
            return False
        async with self._lock:
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
            self._recompute_cleanup_failed_locked()
        return True

    def _enqueue_session_cleanup(self, key: _SessionKey, session: _Session) -> None:
        if key in self._session_cleanup_keys:
            return
        task = asyncio.create_task(
            self._delete_idle_session(key, session),
            name=f"agent_session_cleanup_{secrets.token_hex(8)}",
        )
        self._session_cleanup_keys.add(key)
        self._cleanup_tasks.add(task)
        task.add_done_callback(partial(self._session_cleanup_done, key))
        self._schedule_observation("runtime")

    def _session_cleanup_done(self, key: _SessionKey, task: asyncio.Task[bool]) -> None:
        self._session_cleanup_keys.discard(key)
        self._cleanup_tasks.discard(task)
        if not task.cancelled():
            task.exception()
        self._schedule_observation("runtime")

    def _schedule_expired_sessions_locked(self, now_mono: float) -> None:
        for key, session in tuple(self._sessions.items()):
            expired = (
                session.busy_run_id is None
                and now_mono - session.last_touched_mono >= self._session_ttl_seconds
            )
            retry_failed = session.busy_run_id == "cleanup_failed"
            if not expired and not retry_failed:
                continue
            session.busy_run_id = "deleting"
            self._enqueue_session_cleanup(key, session)

    def _recompute_cleanup_failed_locked(self) -> None:
        self._cleanup_failed = any(
            session.busy_run_id == "cleanup_failed"
            for session in self._sessions.values()
        ) or any(record.cleanup_failed for record in self._runs.values())

    async def _worker_loop(self) -> None:
        while True:
            if self._stopping.is_set() and self._phases.empty():
                return
            try:
                phase = await asyncio.wait_for(self._phases.get(), timeout=0.1)
            except TimeoutError:
                continue
            try:
                await self._execute_phase(phase)
            except asyncio.CancelledError:
                raise
            except Exception:
                try:
                    await self._fail_phase_closed(phase.run_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._cleanup_failed = True
            finally:
                self._phases.task_done()

    async def _fail_phase_closed(self, run_id: str) -> None:
        phase_task: asyncio.Task[WorkflowInvocationResult] | None = None
        cleanup = False
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status in TERMINAL_RUN_STATUSES:
                return
            phase_task = record.phase_task
            record.phase_task = None
            if record.status is AgentRunStatus.CANCELING:
                cleanup = record.session_touched
            else:
                record.error_code = "agent_phase_failed"
                if record.session_touched:
                    record.terminal_after_cleanup = AgentRunStatus.FAILED
                    self._transition(record, AgentRunStatus.CANCELING)
                    cleanup = True
                else:
                    self._finish_without_cleanup(record, AgentRunStatus.FAILED)
        if phase_task is not None and not phase_task.done():
            phase_task.cancel()
            try:
                await phase_task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                pass
        if cleanup:
            await self._cleanup_run(run_id, AgentRunStatus.FAILED)

    async def _execute_phase(self, phase: _QueuedPhase) -> None:
        cleanup_before_start = False
        task: asyncio.Task[WorkflowInvocationResult] | None = None
        expires_mono = 0.0
        phase_timeout = 0.0
        phase_started_mono = 0.0
        async with self._lock:
            record = self._runs.get(phase.run_id)
            if record is None or record.status is not AgentRunStatus.QUEUED:
                return
            now_mono = time.monotonic()
            expires_mono = record.expires_mono
            remaining = expires_mono - now_mono
            if remaining <= 0:
                cleanup_before_start = self._finish_queued(
                    record, AgentRunStatus.EXPIRED
                )
            else:
                tools = self._tools_for(record.application)
                phase_timeout = min(self._config.phase_timeout_seconds, remaining)
                phase_started_mono = time.monotonic()
                record.session_touched = True
                self._transition(record, AgentRunStatus.RUNNING)
                task = asyncio.create_task(
                    self._runtime.invoke(
                        record.application,
                        phase.invocation,
                        identity=record.identity,
                        tools=tools,
                        timeout_seconds=phase_timeout,
                    ),
                    name=f"agent_run_invoke_{record.run_id}_{phase.kind}",
                )
                record.phase_task = task
        if cleanup_before_start:
            await self._cleanup_run(phase.run_id, AgentRunStatus.EXPIRED)
            return
        if task is None:
            return
        execution = await self._wait_for_phase_execution(
            task, timeout_seconds=phase_timeout, expires_mono=expires_mono
        )
        if execution.timed_out:
            phase_outcome = await self._mark_phase_timeout(phase.run_id, execution)
            if phase_outcome is None:
                return
            self._schedule_observation(
                "phase",
                phase_outcome,
                max(0.0, time.monotonic() - phase_started_mono),
            )
            await self._consume_late_phase_task(task)

        finalized = await self._finalize_phase_execution(phase.run_id, task, execution)
        if finalized is None:
            return
        needs_cleanup, terminal, phase_outcome = finalized
        if not execution.timed_out:
            self._schedule_observation(
                "phase",
                phase_outcome,
                max(0.0, time.monotonic() - phase_started_mono),
            )
        if needs_cleanup:
            await self._cleanup_run(phase.run_id, terminal or AgentRunStatus.CANCELED)
            return
        if execution.result is None:
            return
        await self._apply_result(phase.run_id, execution.result)

    async def _wait_for_phase_execution(
        self,
        task: asyncio.Task[WorkflowInvocationResult],
        *,
        timeout_seconds: float,
        expires_mono: float,
    ) -> _PhaseExecution:
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout_seconds)
            if not done:
                task.cancel()
                error_code, terminal = self._phase_timeout_failure(expires_mono)
                return _PhaseExecution(
                    error_code=error_code,
                    error_terminal=terminal,
                    timed_out=True,
                )
            return _PhaseExecution(result=task.result())
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                task.cancel()
                raise
            return _PhaseExecution(error_code="agent_run_canceled")
        except AiTimeoutError:
            error_code, terminal = self._phase_timeout_failure(expires_mono)
            return _PhaseExecution(error_code=error_code, error_terminal=terminal)
        except AiWorkflowActionUnsupportedError:
            return self._phase_error(
                expires_mono, error_code="agent_action_unsupported"
            )
        except Exception:
            return self._phase_error(expires_mono, error_code="agent_execution_failed")

    @staticmethod
    def _phase_timeout_failure(expires_mono: float) -> tuple[str, AgentRunStatus]:
        if time.monotonic() >= expires_mono:
            return "agent_run_expired", AgentRunStatus.EXPIRED
        return "agent_phase_timeout", AgentRunStatus.FAILED

    @staticmethod
    def _phase_error(expires_mono: float, *, error_code: str) -> _PhaseExecution:
        if time.monotonic() >= expires_mono:
            return _PhaseExecution(
                error_code="agent_run_expired",
                error_terminal=AgentRunStatus.EXPIRED,
            )
        return _PhaseExecution(error_code=error_code)

    async def _mark_phase_timeout(
        self, run_id: str, execution: _PhaseExecution
    ) -> str | None:
        async with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                return None
            if current.status is AgentRunStatus.CANCELING:
                return self._terminal_phase_outcome(current.terminal_after_cleanup)
            current.error_code = execution.error_code
            current.terminal_after_cleanup = execution.error_terminal
            self._transition(current, AgentRunStatus.CANCELING)
            if execution.error_terminal is AgentRunStatus.EXPIRED:
                return "expired"
            return "timed_out"

    @staticmethod
    async def _consume_late_phase_task(
        task: asyncio.Task[WorkflowInvocationResult],
    ) -> None:
        try:
            await task
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
        except Exception:
            pass

    async def _finalize_phase_execution(
        self,
        run_id: str,
        task: asyncio.Task[WorkflowInvocationResult],
        execution: _PhaseExecution,
    ) -> tuple[bool, AgentRunStatus | None, str] | None:
        async with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                return None
            if current.phase_task is task:
                current.phase_task = None
            needs_cleanup = current.status is AgentRunStatus.CANCELING
            terminal = current.terminal_after_cleanup
            if not needs_cleanup and execution.error_code is not None:
                current.error_code = execution.error_code
                current.terminal_after_cleanup = execution.error_terminal
                self._transition(current, AgentRunStatus.CANCELING)
                needs_cleanup = True
                terminal = execution.error_terminal
            outcome = self._phase_execution_outcome(
                execution, needs_cleanup=needs_cleanup, terminal=terminal
            )
            return needs_cleanup, terminal, outcome

    def _phase_execution_outcome(
        self,
        execution: _PhaseExecution,
        *,
        needs_cleanup: bool,
        terminal: AgentRunStatus | None,
    ) -> str:
        if needs_cleanup:
            return self._terminal_phase_outcome(terminal)
        if execution.error_code is not None or execution.result is None:
            return "failed"
        if execution.result.state == "succeeded":
            return "succeeded"
        return "requires_action"

    @staticmethod
    def _terminal_phase_outcome(status: AgentRunStatus | None) -> str:
        if status is AgentRunStatus.EXPIRED:
            return "expired"
        if status is AgentRunStatus.CANCELED:
            return "canceled"
        return "failed"

    async def _apply_result(
        self, run_id: str, result: WorkflowInvocationResult
    ) -> None:
        cleanup = False
        cleanup_terminal = AgentRunStatus.FAILED
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status is not AgentRunStatus.RUNNING:
                return
            if not self._accumulate_usage(record, result.usage):
                record.error_code = "agent_invalid_usage"
                record.terminal_after_cleanup = AgentRunStatus.FAILED
                self._transition(record, AgentRunStatus.CANCELING)
                cleanup = True
            elif time.monotonic() >= record.expires_mono:
                record.error_code = "agent_run_expired"
                record.terminal_after_cleanup = AgentRunStatus.EXPIRED
                self._transition(record, AgentRunStatus.CANCELING)
                cleanup = True
                cleanup_terminal = AgentRunStatus.EXPIRED
            elif result.state == "succeeded":
                if result.output_format != record.application.output_format:
                    record.error_code = "agent_invalid_result"
                    record.terminal_after_cleanup = AgentRunStatus.FAILED
                    self._transition(record, AgentRunStatus.CANCELING)
                    cleanup = True
                else:
                    try:
                        if result.output_format == "text":
                            if not isinstance(result.output, str):
                                raise AgentRunPayloadError(
                                    "Agent output format is invalid"
                                )
                        elif isinstance(result.output, str):
                            # A JSON application may intentionally return a JSON string.
                            pass
                        self._validate_payload(result.output)
                    except AgentRunPayloadError:
                        record.error_code = "agent_output_too_large"
                        record.terminal_after_cleanup = AgentRunStatus.FAILED
                        self._transition(record, AgentRunStatus.CANCELING)
                        cleanup = True
                    else:
                        if time.monotonic() >= record.expires_mono:
                            record.error_code = "agent_run_expired"
                            record.terminal_after_cleanup = AgentRunStatus.EXPIRED
                            self._transition(record, AgentRunStatus.CANCELING)
                            cleanup = True
                            cleanup_terminal = AgentRunStatus.EXPIRED
                        else:
                            record.output = thaw_json(result.output)
                            record.model = result.model
                            record.finish_reason = result.finish_reason
                            self._finish_success(record)
            elif not record.application.resumable:
                record.error_code = "agent_action_unsupported"
                record.terminal_after_cleanup = AgentRunStatus.FAILED
                self._transition(record, AgentRunStatus.CANCELING)
                cleanup = True
            elif record.resume_count >= self._config.max_resumes:
                record.error_code = "agent_resume_limit_exceeded"
                record.terminal_after_cleanup = AgentRunStatus.FAILED
                self._transition(record, AgentRunStatus.CANCELING)
                cleanup = True
            else:
                now = datetime.now(UTC)
                now_mono = time.monotonic()
                action_expiry_mono = min(
                    record.expires_mono,
                    now_mono + self._config.action_timeout_seconds,
                )
                action_expiry = now + timedelta(
                    seconds=max(0.0, action_expiry_mono - now_mono)
                )
                action_specs = {
                    action.code: action for action in record.application.action_specs
                }
                if (
                    not 1 <= len(result.actions) <= 128
                    or len({action.runtime_action_id for action in result.actions})
                    != len(result.actions)
                    or any(
                        action.code not in action_specs
                        or action_specs[action.code].kind != action.kind
                        for action in result.actions
                    )
                ):
                    record.error_code = "agent_action_unsupported"
                    record.terminal_after_cleanup = AgentRunStatus.FAILED
                    self._transition(record, AgentRunStatus.CANCELING)
                    cleanup = True
                else:
                    if time.monotonic() >= record.expires_mono:
                        record.error_code = "agent_run_expired"
                        record.terminal_after_cleanup = AgentRunStatus.EXPIRED
                        self._transition(record, AgentRunStatus.CANCELING)
                        cleanup = True
                        cleanup_terminal = AgentRunStatus.EXPIRED
                    else:
                        record.pending_actions = tuple(
                            _PendingAction(
                                public_id=f"act_{secrets.token_hex(16)}",
                                runtime_id=action.runtime_action_id,
                                code=action.code,
                                kind=action.kind,
                                expires_at=action_expiry,
                                expires_mono=action_expiry_mono,
                            )
                            for action in result.actions
                        )
                        record.resume_token = result.resume_token
                        self._transition(record, AgentRunStatus.REQUIRES_ACTION)
        if cleanup:
            await self._cleanup_run(run_id, cleanup_terminal)

    @staticmethod
    def _accumulate_usage(record: _RunRecord, usage: TokenUsage) -> bool:
        values = (
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            return False
        if any(value < 0 for value in values):
            return False
        if usage.total_tokens < usage.input_tokens + usage.output_tokens:
            return False
        previous = record.usage or TokenUsage()
        try:
            record.usage = TokenUsage(
                input_tokens=previous.input_tokens + usage.input_tokens,
                output_tokens=previous.output_tokens + usage.output_tokens,
                total_tokens=previous.total_tokens + usage.total_tokens,
            )
        except Exception:
            return False
        return True

    async def _cleanup_run(self, run_id: str, terminal: AgentRunStatus) -> None:
        task = self._ensure_run_cleanup_task(run_id, terminal)
        await asyncio.shield(task)

    async def _cleanup_run_once(self, run_id: str, terminal: AgentRunStatus) -> None:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status in TERMINAL_RUN_STATUSES:
                return
            application = record.application
            identity = record.identity
            session_id = record.session_key.session_id
        try:
            async with asyncio.timeout(self._config.phase_timeout_seconds):
                await self._runtime.delete_session(
                    application, identity=identity, session_id=session_id
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current.cleanup_failed = True
                    self._cleanup_failed = True
            return
        async with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                return
            if current.status in TERMINAL_RUN_STATUSES:
                return
            self._sessions.pop(current.session_key, None)
            current.cleanup_failed = False
            self._finish_terminal(current, current.terminal_after_cleanup or terminal)
            self._recompute_cleanup_failed_locked()

    def _ensure_run_cleanup_task(
        self, run_id: str, terminal: AgentRunStatus
    ) -> asyncio.Task[None]:
        existing = self._run_cleanup_tasks.get(run_id)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._cleanup_run_once(run_id, terminal),
            name=f"agent_run_cleanup_{run_id}",
        )
        self._run_cleanup_tasks[run_id] = task
        self._cleanup_tasks.add(task)
        task.add_done_callback(partial(self._cleanup_done, run_id))
        self._schedule_observation("runtime")
        return task

    def _enqueue_cleanup(self, run_id: str, terminal: AgentRunStatus) -> None:
        self._ensure_run_cleanup_task(run_id, terminal)

    def _cleanup_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._run_cleanup_tasks.get(run_id) is task:
            self._run_cleanup_tasks.pop(run_id, None)
        self._cleanup_tasks.discard(task)
        if not task.cancelled():
            task.exception()
        self._schedule_observation("runtime")

    async def _sweeper_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._config.poll_interval_seconds,
                )
            except TimeoutError:
                pass
            if self._stopping.is_set():
                return
            await self._expire_runs()

    async def _expire_runs(self) -> None:
        cancel_tasks: list[asyncio.Task[WorkflowInvocationResult]] = []
        cleanup_runs: list[tuple[str, AgentRunStatus]] = []
        now_mono = time.monotonic()
        async with self._lock:
            self._prune_terminal_locked(now_mono)
            for record in self._runs.values():
                if (
                    record.status is AgentRunStatus.CANCELING
                    and record.cleanup_failed
                    and record.phase_task is None
                ):
                    cleanup_runs.append(
                        (
                            record.run_id,
                            record.terminal_after_cleanup or AgentRunStatus.FAILED,
                        )
                    )
                    continue
                action_expired = record.pending_actions and any(
                    now_mono >= action.expires_mono for action in record.pending_actions
                )
                if now_mono < record.expires_mono and not action_expired:
                    continue
                if record.status in TERMINAL_RUN_STATUSES:
                    continue
                if record.status is AgentRunStatus.CANCELING:
                    continue
                if record.status is AgentRunStatus.QUEUED:
                    if self._finish_queued(record, AgentRunStatus.EXPIRED):
                        cleanup_runs.append((record.run_id, AgentRunStatus.EXPIRED))
                    continue
                record.error_code = "agent_run_expired"
                record.terminal_after_cleanup = AgentRunStatus.EXPIRED
                self._transition(record, AgentRunStatus.CANCELING)
                if record.phase_task is not None and not record.phase_task.done():
                    cancel_tasks.append(record.phase_task)
                else:
                    record.phase_task = None
                    cleanup_runs.append((record.run_id, AgentRunStatus.EXPIRED))
        for task in cancel_tasks:
            task.cancel()
        for run_id, terminal in cleanup_runs:
            self._enqueue_cleanup(run_id, terminal)

    async def _close_once(self) -> bool:
        if not self._stopping.is_set():
            await self.drain()
        self._stopping.set()
        phase_tasks: list[asyncio.Task[WorkflowInvocationResult]] = []
        cleanup_runs: list[tuple[str, AgentRunStatus]] = []
        async with self._lock:
            for record in self._runs.values():
                phase_task, cleanup_run = self._prepare_run_for_shutdown(record)
                if phase_task is not None:
                    phase_tasks.append(phase_task)
                if cleanup_run is not None:
                    cleanup_runs.append(cleanup_run)
            for key, session in tuple(self._sessions.items()):
                if session.busy_run_id not in {None, "cleanup_failed"}:
                    continue
                session.busy_run_id = "deleting"
                self._enqueue_session_cleanup(key, session)
        for task in phase_tasks:
            task.cancel()
        for run_id, terminal in cleanup_runs:
            self._enqueue_cleanup(run_id, terminal)
        sweeper = self._sweeper
        if sweeper is not None and not sweeper.done():
            sweeper.cancel()
        tasks = [*self._workers, *self._cleanup_tasks]
        if sweeper is not None:
            tasks.append(sweeper)
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=min(5.0, max(0.2, self._config.phase_timeout_seconds)),
            )
            for task in done:
                if not task.cancelled():
                    try:
                        task.exception()
                    except Exception:
                        pass
            if pending:
                return False
        async with self._lock:
            if (
                any(
                    record.status not in TERMINAL_RUN_STATUSES
                    for record in self._runs.values()
                )
                or self._sessions
            ):
                return False
        self._started = False
        self._draining = False
        self._schedule_observation("runtime")
        await asyncio.sleep(0)
        return True

    def _prepare_run_for_shutdown(
        self, record: _RunRecord
    ) -> tuple[
        asyncio.Task[WorkflowInvocationResult] | None,
        tuple[str, AgentRunStatus] | None,
    ]:
        if record.status in TERMINAL_RUN_STATUSES:
            return None, None
        if record.status is AgentRunStatus.QUEUED:
            cleanup = self._finish_queued(record, AgentRunStatus.CANCELED)
            return (
                None,
                (record.run_id, AgentRunStatus.CANCELED) if cleanup else None,
            )
        if record.status is not AgentRunStatus.CANCELING:
            record.terminal_after_cleanup = AgentRunStatus.CANCELED
            self._transition(record, AgentRunStatus.CANCELING)
        if record.phase_task is not None and not record.phase_task.done():
            return record.phase_task, None
        record.phase_task = None
        return (
            None,
            (
                record.run_id,
                record.terminal_after_cleanup or AgentRunStatus.CANCELED,
            ),
        )

    def _tools_for(self, application: AdkApplicationSpec) -> tuple[AgentToolSpec, ...]:
        try:
            return tuple(self._tools[name] for name in application.tool_names)
        except KeyError:
            raise AgentRunsUnavailableError(
                "Agent application tools are unavailable"
            ) from None

    def _finish_success(self, record: _RunRecord) -> None:
        session = self._sessions.get(record.session_key)
        if session is not None and session.busy_run_id == record.run_id:
            session.busy_run_id = None
            session.last_touched_mono = time.monotonic()
            session.last_run_id = record.run_id
        self._finish_terminal(record, AgentRunStatus.SUCCEEDED)

    def _finish_without_cleanup(
        self, record: _RunRecord, status: AgentRunStatus
    ) -> None:
        session = self._sessions.get(record.session_key)
        if record.session_was_new:
            self._sessions.pop(record.session_key, None)
        elif session is not None and session.busy_run_id == record.run_id:
            session.busy_run_id = None
        self._finish_terminal(record, status)

    def _finish_queued(self, record: _RunRecord, status: AgentRunStatus) -> bool:
        if status is AgentRunStatus.EXPIRED:
            record.error_code = "agent_run_expired"
        if record.session_touched:
            record.terminal_after_cleanup = status
            self._transition(record, AgentRunStatus.CANCELING)
            return True
        self._finish_without_cleanup(record, status)
        return False

    def _finish_terminal(self, record: _RunRecord, status: AgentRunStatus) -> None:
        record.pending_actions = ()
        record.resume_token = None
        record.terminal_after_cleanup = None
        record.terminal_mono = time.monotonic()
        self._transition(record, status)

    def _transition(self, record: _RunRecord, status: AgentRunStatus) -> None:
        record.status = status
        record.version += 1
        record.updated_at = datetime.now(UTC)
        self._refresh_drained_locked()
        self._schedule_observation("transition", status.value)
        if status in TERMINAL_RUN_STATUSES:
            self._schedule_observation("terminal", status.value)

    def _refresh_drained_locked(self) -> None:
        if all(
            record.status in TERMINAL_RUN_STATUSES for record in self._runs.values()
        ):
            self._drained.set()
        else:
            self._drained.clear()

    def _prune_terminal_locked(self, now_mono: float) -> None:
        expired = [
            run_id
            for run_id, record in self._runs.items()
            if record.terminal_mono is not None
            and now_mono - record.terminal_mono
            >= self._config.terminal_retention_seconds
        ]
        for run_id in expired:
            self._runs.pop(run_id, None)
        if expired:
            removed = set(expired)
            self._idempotency = {
                key: value
                for key, value in self._idempotency.items()
                if value.run_id not in removed
            }
            self._schedule_observation("runtime")
        self._schedule_expired_sessions_locked(now_mono)

    def _owned_run(self, run_id: str, identity: AgentIdentity) -> _RunRecord:
        record = self._runs.get(run_id)
        if record is None or record.identity.principal_id != identity.principal_id:
            raise AgentRunNotFoundError("Agent run was not found")
        return record

    def _snapshot(self, record: _RunRecord) -> AgentRunSnapshot:
        return AgentRunSnapshot(
            run_id=record.run_id,
            agent_id=record.application.agent_id,
            agent_revision=record.application.revision,
            session_id=record.session_key.session_id,
            status=record.status,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            output=cast(JsonValue, record.output),
            usage=record.usage,
            model=record.model,
            finish_reason=record.finish_reason,
            actions=(
                tuple(
                    AgentRunAction(
                        action_id=action.public_id,
                        code=action.code,
                        type=action.kind,
                        response_schema=thaw_json(
                            next(
                                spec.response_schema
                                for spec in record.application.action_specs
                                if spec.code == action.code
                            )
                        ),
                        expires_at=action.expires_at,
                    )
                    for action in record.pending_actions
                )
                if record.status is AgentRunStatus.REQUIRES_ACTION
                else ()
            ),
            error=(
                AgentRunError(code=record.error_code)
                if record.error_code is not None
                else None
            ),
        )

    def _ensure_operational(self) -> None:
        if not self._started or self._cleanup_failed or self._stopping.is_set():
            raise AgentRunsUnavailableError("Agent Runs service is unavailable")

    def _ensure_accepting(self) -> None:
        self._ensure_operational()
        if not self._accepting or self._draining:
            raise AgentRunsUnavailableError("Agent Runs service is unavailable")

    def _capacity_error(self, message: str) -> AgentRunsCapacityError:
        self._schedule_observation("capacity")
        return AgentRunsCapacityError(message)

    def _validate_payload(self, value: object) -> None:
        try:
            if isinstance(value, str):
                encoded = value.encode("utf-8")
            else:
                encoded = json.dumps(
                    thaw_json(value),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise AgentRunPayloadError(
                "Agent run payload must be strict JSON"
            ) from None
        if len(encoded) > self._config.max_payload_bytes:
            raise AgentRunPayloadError("Agent run payload is too large")

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if (
            not value
            or len(value) > 128
            or not value.isascii()
            or any(character.isspace() for character in value)
        ):
            raise AgentRunConflictError(
                "Idempotency key is invalid", code="idempotency_conflict"
            )

    def _digest(self, value: object) -> bytes:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(self._digest_key, encoded, hashlib.sha256).digest()
