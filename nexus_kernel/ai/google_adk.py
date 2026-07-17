"""Google ADK agent runtime with trusted identities and bounded sessions."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from jsonschema.exceptions import (  # type: ignore[import-untyped]
    ValidationError as JsonSchemaValidationError,
)
from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]

from nexus_kernel.ai._closing import (
    ActiveCallGate,
    CallAdmissionError,
    CloseCoordinator,
    observe_task,
)
from nexus_kernel.ai.adk_sessions import (
    AdkSessionLease,
    AdkSessionStore,
    BoundedMemoryAdkSessionStore,
)
from nexus_kernel.ai.contracts import (
    AgentIdentity,
    AgentToolSpec,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    SessionKey,
    TokenUsage,
    thaw_json,
)
from nexus_kernel.ai.errors import (
    AiProviderError,
    AiTimeoutError,
    AiToolError,
    AiWorkflowActionUnsupportedError,
)
from nexus_kernel.ai.tool_executor import ToolExecutor
from nexus_kernel.ai.workflows import (
    AdkApplicationBuildContext,
    AdkApplicationSpec,
    JsonValue,
    WorkflowActionResponse,
    WorkflowFunctionNodeSpec,
    WorkflowInvocation,
    WorkflowInvocationResult,
    WorkflowNodeResult,
    WorkflowPendingAction,
    WorkflowRequestInputNodeSpec,
    WorkflowTransformNodeSpec,
    _callable_semantic_digest,
    _freeze_strict_json,
    _workflow_function_node_material,
)


_REQUEST_INPUT_FUNCTION = "adk_request_input"
_MAX_WORKFLOW_TRANSFORM_BYTES = 64 * 1024
_WORKFLOW_CALLBACK_FIELDS = (
    "before_agent_callback",
    "after_agent_callback",
    "before_model_callback",
    "after_model_callback",
    "on_model_error_callback",
    "before_tool_callback",
    "after_tool_callback",
    "on_tool_error_callback",
)


def _canonical_workflow_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            value = model_dump(mode="json", exclude_none=False)
        except (TypeError, ValueError):
            raise AiProviderError(
                "Google ADK application configuration is unsupported"
            ) from None
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise AiProviderError("Google ADK application configuration is unsupported")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise AiProviderError("Google ADK application configuration is unsupported")
        return {
            key: _canonical_workflow_value(item) for key, item in sorted(value.items())
        }
    if isinstance(value, list | tuple):
        return [_canonical_workflow_value(item) for item in value]
    raise AiProviderError("Google ADK application configuration is unsupported")


def _canonical_workflow_schema(value: Any) -> Any:
    if value is None or isinstance(value, Mapping | bool):
        return _canonical_workflow_value(value)
    if not isinstance(value, type) and callable(getattr(value, "model_dump", None)):
        return _canonical_workflow_value(value)
    model_json_schema = getattr(value, "model_json_schema", None)
    if callable(model_json_schema):
        try:
            return _canonical_workflow_value(model_json_schema())
        except (TypeError, ValueError):
            raise AiProviderError(
                "Google ADK application schema is unsupported"
            ) from None
    try:
        from pydantic import TypeAdapter

        return _canonical_workflow_value(TypeAdapter(value).json_schema())
    except (TypeError, ValueError):
        raise AiProviderError("Google ADK application schema is unsupported") from None


def _workflow_configuration_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            _canonical_workflow_value(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise AiProviderError(
            "Google ADK application configuration is unsupported"
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def _token_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AiProviderError("Google ADK returned invalid token usage")
    return value


def _accumulate_token_usage(
    current: tuple[int, int, int], usage: Any
) -> tuple[int, int, int]:
    input_tokens = _token_count(usage.prompt_token_count) + _token_count(
        getattr(usage, "tool_use_prompt_token_count", None)
    )
    output_tokens = _token_count(usage.candidates_token_count) + _token_count(
        getattr(usage, "thoughts_token_count", None)
    )
    return (
        current[0] + input_tokens,
        current[1] + output_tokens,
        current[2] + _token_count(usage.total_token_count),
    )


def _validate_adk_telemetry_environment() -> None:
    if os.getenv("ADK_TELEMETRY_IGNORE_RUN_CONFIG", "").strip().lower() not in {
        "1",
        "true",
    }:
        return

    from google.adk.telemetry import TelemetryConfig

    effective = TelemetryConfig()
    if (
        effective.should_add_content_to_logs
        or effective.should_add_content_to_experimental_spans
        or effective.should_add_content_to_legacy_spans
    ):
        raise AiProviderError("Google ADK telemetry content capture is unsafe")


def _session_event_count(session: Any) -> int:
    events = getattr(session, "events", ())
    try:
        count = len(events)
    except (TypeError, ValueError, OverflowError):
        raise AiProviderError("Google ADK returned invalid session events") from None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise AiProviderError("Google ADK returned invalid session events")
    return count


def _public_event_text(event: Any) -> str:
    content = event.content
    if content is None:
        return ""
    return "".join(
        part.text or ""
        for part in content.parts or []
        if getattr(part, "thought", False) is not True
    )


def _workflow_definition_fingerprint(
    application: AdkApplicationSpec, *, model: str
) -> str:
    material = f"{application.fingerprint}\0{model}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _runtime_workflow_app_name(application: AdkApplicationSpec, *, model: str) -> str:
    fingerprint = _workflow_definition_fingerprint(application, model=model)
    return f"nexus_kernel_{application.agent_id}_{fingerprint}"


def _workflow_session_key(
    application: AdkApplicationSpec,
    *,
    identity: AgentIdentity,
    session_id: str,
    model: str | None = None,
) -> SessionKey:
    definition_fingerprint = (
        application.fingerprint
        if model is None
        else _workflow_definition_fingerprint(application, model=model)
    )
    material = (
        f"{application.agent_id}\0{definition_fingerprint}\0{session_id}"
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return SessionKey(identity.principal_id, f"wf:{digest}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _parse_workflow_json(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise AiProviderError("Google ADK returned invalid JSON output") from None


def _runtime_identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise AiProviderError("Google ADK returned an invalid runtime identifier")
    return value


def _workflow_response_payload(response: WorkflowActionResponse) -> dict[str, Any]:
    value = thaw_json(response.value)
    if isinstance(value, dict):
        return value
    return {"result": value}


def _freeze_workflow_node_input(
    value: Any,
    *,
    content_type: type,
    depth: int = 0,
) -> Any:
    if depth > 32:
        raise ValueError("workflow transform input exceeds the maximum depth")
    if isinstance(value, content_type):
        content = cast(Any, value)
        text: list[str] = []
        for part in content.parts or ():
            if getattr(part, "thought", False) is True:
                continue
            if not isinstance(part.text, str):
                raise ValueError("workflow transform input must be strict JSON")
            text.append(part.text)
        value = "".join(text)
    elif isinstance(value, Mapping):
        value = {
            key: _freeze_workflow_node_input(
                item,
                content_type=content_type,
                depth=depth + 1,
            )
            for key, item in value.items()
        }
    elif isinstance(value, list):
        value = [
            _freeze_workflow_node_input(
                item,
                content_type=content_type,
                depth=depth + 1,
            )
            for item in value
        ]
    if depth == 0:
        return _freeze_strict_json(value)
    return value


def _workflow_json_size(value: Any) -> int:
    try:
        if isinstance(value, str):
            return len(value.encode("utf-8"))
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("workflow transform output must be strict JSON") from None


@dataclass(frozen=True, slots=True)
class AdkResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str | None = None
    event_count: int = 1


@dataclass(slots=True)
class _RunnerCleanup:
    runner: Any
    close_task: asyncio.Task[None]
    supervisor: asyncio.Task[None]


@dataclass(frozen=True, slots=True)
class _WorkflowOutputCandidate:
    value: Any
    text: bool
    model: str
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class _ManagedWorkflowFunctionNode:
    spec: WorkflowFunctionNodeSpec
    function: Callable[..., Any]


class AdkBackend(Protocol):
    async def run(
        self,
        *,
        system_instruction: str,
        history: Sequence[ChatMessage],
        new_message: ChatMessage,
        user_id: str,
        session_id: str,
        tool_executor: ToolExecutor,
        max_llm_calls: int,
    ) -> AdkResult: ...

    async def delete_session(self, key: SessionKey) -> None: ...

    async def close(self) -> None: ...


class _WorkflowAdkBackend(Protocol):
    async def invoke_workflow(
        self,
        *,
        application: AdkApplicationSpec,
        invocation: WorkflowInvocation,
        app_name: str,
        user_id: str,
        session_id: str,
        tool_executor: ToolExecutor,
        max_llm_calls: int,
    ) -> WorkflowInvocationResult: ...

    async def delete_workflow_session(
        self, key: SessionKey, *, app_name: str
    ) -> None: ...


class _SdkAdkBackend:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        app_name: str = "nexus_kernel",
        max_workflow_concurrency: int = 4,
    ) -> None:
        from google.adk.models.google_llm import Gemini
        from google.adk.sessions import InMemorySessionService
        from google.genai import Client, types

        _validate_adk_telemetry_environment()
        http_options = types.HttpOptions(base_url=base_url) if base_url else None
        self._client = Client(api_key=api_key, http_options=http_options)
        explicit_client = self._client

        class ExplicitCredentialGemini(Gemini):
            @property
            def api_client(self) -> Any:
                return explicit_client

        self._model = ExplicitCredentialGemini(model=model)
        self._model_name = model
        self._sessions = InMemorySessionService()  # type: ignore[no-untyped-call]
        self._app_name = app_name
        self._agent_name = "nexus_kernel_agent"
        self._runner_cleanups: dict[int, _RunnerCleanup] = {}
        self._runner_cleanup_timeout_seconds = 2.0
        self._max_workflow_concurrency = max_workflow_concurrency
        self._application_topologies: dict[tuple[str, str], str] = {}

    @property
    def has_unresolved_runner_cleanup(self) -> bool:
        self._prune_runner_cleanups()
        return bool(self._runner_cleanups)

    async def run(
        self,
        *,
        system_instruction: str,
        history: Sequence[ChatMessage],
        new_message: ChatMessage,
        user_id: str,
        session_id: str,
        tool_executor: ToolExecutor,
        max_llm_calls: int,
    ) -> AdkResult:
        from google.adk.agents import LlmAgent
        from google.adk.agents.run_config import RunConfig
        from google.adk.apps import App
        from google.adk.events import Event
        from google.adk.runners import Runner
        from google.adk.telemetry import ContentCapturingMode, TelemetryConfig
        from google.genai import types

        existing = await self._sessions.get_session(
            app_name=self._app_name, user_id=user_id, session_id=session_id
        )
        if existing is None:
            session = await self._sessions.create_session(
                app_name=self._app_name, user_id=user_id, session_id=session_id
            )
            initial_event_count = _session_event_count(session)
            for message in history:
                role = "model" if message.role == "assistant" else "user"
                author = self._agent_name if role == "model" else "user"
                event = Event(
                    author=author,
                    content=types.Content(
                        role=role, parts=[types.Part(text=message.content)]
                    ),
                )
                await self._sessions.append_event(session, event)
        else:
            initial_event_count = _session_event_count(existing)

        agent = LlmAgent(
            name=self._agent_name,
            model=self._model,
            instruction=system_instruction,
            tools=[
                self._tool_adapter(tool, tool_executor) for tool in tool_executor.tools
            ],
        )
        app = App(name=self._app_name, root_agent=agent)
        runner = Runner(
            app=app,
            session_service=self._sessions,
        )
        final_text = ""
        input_tokens = output_tokens = total_tokens = 0
        event_count = 0
        final_model = self._model_name
        finish_reason: str | None = None
        saw_agent_error = False
        primary_error: BaseException | None = None
        try:
            events = runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text=new_message.content)]
                ),
                run_config=RunConfig(
                    max_llm_calls=max_llm_calls,
                    telemetry=TelemetryConfig(
                        capture_message_content=ContentCapturingMode.NO_CONTENT
                    ),
                ),
            )
            async for event in events:
                if event.error_code:
                    saw_agent_error = True
                usage = event.usage_metadata
                if usage is not None:
                    input_tokens, output_tokens, total_tokens = _accumulate_token_usage(
                        (input_tokens, output_tokens, total_tokens), usage
                    )
                if event.author == self._agent_name and event.is_final_response():
                    candidate_text = _public_event_text(event)
                    if candidate_text:
                        final_text = candidate_text
                        model_version = getattr(event, "model_version", None)
                        if isinstance(model_version, str) and model_version.strip():
                            final_model = model_version
                        event_finish_reason = event.finish_reason
                        finish_reason = (
                            str(
                                getattr(
                                    event_finish_reason,
                                    "value",
                                    event_finish_reason,
                                )
                            )
                            if event_finish_reason is not None
                            else None
                        )
            if not final_text:
                if saw_agent_error:
                    raise AiProviderError("Google ADK returned an agent error")
                raise AiProviderError("Google ADK returned no text content")
            updated = await self._sessions.get_session(
                app_name=self._app_name,
                user_id=user_id,
                session_id=session_id,
            )
            if updated is None:
                raise AiProviderError("Google ADK session disappeared")
            final_event_count = _session_event_count(updated)
            if final_event_count < initial_event_count:
                raise AiProviderError("Google ADK returned invalid session events")
            event_count = final_event_count - initial_event_count
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup = self._schedule_runner_cleanup(runner)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
            except Exception:
                if primary_error is None:
                    raise AiProviderError("Google ADK Runner cleanup failed") from None
        return AdkResult(
            text=final_text,
            model=final_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=self._validated_total_usage(
                (input_tokens, output_tokens, total_tokens)
            ),
            finish_reason=finish_reason,
            event_count=event_count,
        )

    async def invoke_workflow(
        self,
        *,
        application: AdkApplicationSpec,
        invocation: WorkflowInvocation,
        app_name: str,
        user_id: str,
        session_id: str,
        tool_executor: ToolExecutor,
        max_llm_calls: int,
    ) -> WorkflowInvocationResult:
        from google.adk.runners import Runner
        from google.genai import types

        app, root_name, terminal_node_name, agent_control_names = (
            self._build_workflow_app(
                application,
                app_name=app_name,
                tool_executor=tool_executor,
            )
        )
        session, invocation_id, new_message = await self._prepare_workflow_run(
            application,
            invocation,
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            types=types,
        )
        initial_event_count = _session_event_count(session)
        runner = Runner(app=app, session_service=self._sessions)
        primary_error: BaseException | None = None
        try:
            return await self._consume_workflow_run(
                application,
                runner=runner,
                root_name=root_name,
                terminal_node_name=terminal_node_name,
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                invocation_id=invocation_id,
                new_message=new_message,
                max_llm_calls=max_llm_calls,
                initial_event_count=initial_event_count,
                agent_control_names=agent_control_names,
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup = self._schedule_runner_cleanup(runner)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
            except Exception:
                if primary_error is None:
                    raise AiProviderError("Google ADK Runner cleanup failed") from None

    async def _prepare_workflow_run(
        self,
        application: AdkApplicationSpec,
        invocation: WorkflowInvocation,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        types: Any,
    ) -> tuple[Any, str | None, Any]:
        session = await self._sessions.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            if invocation.resume_token is not None:
                raise AiProviderError("Google ADK workflow session is unavailable")
            session = await self._sessions.create_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )
        if invocation.resume_token is None:
            message = types.Content(
                role="user",
                parts=[types.Part(text=invocation.text)],
            )
            return session, None, message

        invocation_id = _runtime_identifier(invocation.resume_token)
        pending = self._pending_actions_for_resume(
            application,
            session,
            invocation_id=invocation_id,
        )
        if len({item.runtime_action_id for item in invocation.responses}) != len(
            invocation.responses
        ):
            raise AiProviderError("Google ADK workflow resume actions changed")
        responses = {
            response.runtime_action_id: response for response in invocation.responses
        }
        if responses.keys() != pending.keys():
            raise AiProviderError("Google ADK workflow resume actions changed")
        parts = [
            self._workflow_response_part(
                application,
                pending[runtime_id],
                responses[runtime_id],
                types=types,
            )
            for runtime_id in pending
        ]
        return session, invocation_id, types.Content(role="user", parts=parts)

    @staticmethod
    def _workflow_response_part(
        application: AdkApplicationSpec,
        action: WorkflowPendingAction,
        response: WorkflowActionResponse,
        *,
        types: Any,
    ) -> Any:
        if response.kind != action.kind:
            raise AiProviderError("Google ADK workflow resume action changed")
        spec = next(
            item for item in application.action_specs if item.code == action.code
        )
        try:
            Draft202012Validator(thaw_json(spec.response_schema)).validate(
                thaw_json(response.value)
            )
        except JsonSchemaValidationError:
            raise AiProviderError(
                "Google ADK workflow resume response is invalid"
            ) from None
        return types.Part(
            function_response=types.FunctionResponse(
                id=action.runtime_action_id,
                name=_REQUEST_INPUT_FUNCTION,
                response=_workflow_response_payload(response),
            )
        )

    async def _consume_workflow_run(
        self,
        application: AdkApplicationSpec,
        *,
        runner: Any,
        root_name: str,
        terminal_node_name: str | None,
        app_name: str,
        user_id: str,
        session_id: str,
        invocation_id: str | None,
        new_message: Any,
        max_llm_calls: int,
        initial_event_count: int,
        agent_control_names: frozenset[str],
    ) -> WorkflowInvocationResult:
        from google.adk.agents.run_config import RunConfig
        from google.adk.telemetry import ContentCapturingMode, TelemetryConfig

        usage = (0, 0, 0)
        output_candidates: list[_WorkflowOutputCandidate] = []
        pending_actions: list[WorkflowPendingAction] = []
        resume_tokens: set[str] = set()
        saw_agent_error = False
        events = runner.run_async(
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
            new_message=new_message,
            run_config=RunConfig(
                max_llm_calls=max_llm_calls,
                telemetry=TelemetryConfig(
                    capture_message_content=ContentCapturingMode.NO_CONTENT
                ),
            ),
        )
        async for event in events:
            self._reject_unsupported_workflow_actions(
                event, agent_control_names=agent_control_names
            )
            saw_agent_error = saw_agent_error or bool(event.error_code)
            if event.usage_metadata is not None:
                usage = _accumulate_token_usage(usage, event.usage_metadata)
            event_actions = self._pending_actions_from_event(application, event)
            if event_actions:
                pending_actions.extend(event_actions)
                resume_tokens.add(_runtime_identifier(event.invocation_id))
                continue
            candidate = self._workflow_output_from_event(
                event,
                root_name=root_name,
                terminal_node_name=terminal_node_name,
            )
            if candidate is not None:
                output_candidates.append(candidate)

        updated = await self._sessions.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if updated is None:
            raise AiProviderError("Google ADK workflow session disappeared")
        final_event_count = _session_event_count(updated)
        if final_event_count < initial_event_count:
            raise AiProviderError("Google ADK returned invalid session events")
        return self._normalize_workflow_result(
            application,
            usage=usage,
            event_count=final_event_count - initial_event_count,
            output_candidates=output_candidates,
            pending_actions=pending_actions,
            resume_tokens=resume_tokens,
            saw_agent_error=saw_agent_error,
        )

    def _normalize_workflow_result(
        self,
        application: AdkApplicationSpec,
        *,
        usage: tuple[int, int, int],
        event_count: int,
        output_candidates: list[_WorkflowOutputCandidate],
        pending_actions: list[WorkflowPendingAction],
        resume_tokens: set[str],
        saw_agent_error: bool,
    ) -> WorkflowInvocationResult:
        normalized_usage = TokenUsage(
            input_tokens=usage[0],
            output_tokens=usage[1],
            total_tokens=self._validated_total_usage(usage),
        )
        if pending_actions:
            if not application.resumable or len(resume_tokens) != 1:
                raise AiWorkflowActionUnsupportedError(
                    "Google ADK returned unsupported actions"
                )
            runtime_ids = [action.runtime_action_id for action in pending_actions]
            if len(set(runtime_ids)) != len(runtime_ids) or output_candidates:
                raise AiWorkflowActionUnsupportedError(
                    "Google ADK returned ambiguous actions"
                )
            return WorkflowInvocationResult(
                state="requires_action",
                usage=normalized_usage,
                model=self._model_name,
                event_count=event_count,
                actions=tuple(pending_actions),
                resume_token=next(iter(resume_tokens)),
            )

        if len(output_candidates) != 1:
            if saw_agent_error and not output_candidates:
                raise AiProviderError("Google ADK returned an agent error")
            raise AiProviderError("Google ADK returned ambiguous workflow output")
        candidate = output_candidates[0]
        output = candidate.value
        if application.output_format == "json" and candidate.text:
            output = _parse_workflow_json(cast(str, output))
        return WorkflowInvocationResult(
            state="succeeded",
            output=output,
            output_format=application.output_format,
            usage=normalized_usage,
            model=candidate.model,
            finish_reason=candidate.finish_reason,
            event_count=event_count,
        )

    @staticmethod
    def _validated_total_usage(usage: tuple[int, int, int]) -> int:
        if usage[2] < usage[0] + usage[1]:
            raise AiProviderError("Google ADK returned inconsistent token usage")
        return usage[2]

    def _build_workflow_function_nodes(
        self,
        application: AdkApplicationSpec,
    ) -> tuple[dict[str, Any], dict[int, _ManagedWorkflowFunctionNode]]:
        from google.adk.events import Event, RequestInput
        from google.adk.workflow import FunctionNode, RetryConfig
        from google.genai import types

        action_specs = {action.code: action for action in application.action_specs}
        nodes: dict[str, Any] = {}
        specs_by_id: dict[int, _ManagedWorkflowFunctionNode] = {}
        for spec in application.function_nodes:
            if isinstance(spec, WorkflowTransformNodeSpec):
                input_validator = Draft202012Validator(thaw_json(spec.input_schema))
                output_validator = Draft202012Validator(thaw_json(spec.output_schema))

                async def transform_node(
                    node_input: Any,
                    *,
                    _spec: WorkflowTransformNodeSpec = spec,
                    _input_validator: Draft202012Validator = input_validator,
                    _output_validator: Draft202012Validator = output_validator,
                ) -> Any:
                    try:
                        frozen_input = cast(
                            JsonValue,
                            _freeze_workflow_node_input(
                                node_input,
                                content_type=types.Content,
                            ),
                        )
                        if (
                            _workflow_json_size(thaw_json(frozen_input))
                            > _MAX_WORKFLOW_TRANSFORM_BYTES
                        ):
                            raise ValueError("transform input is too large")
                        _input_validator.validate(thaw_json(frozen_input))
                        result = await asyncio.to_thread(
                            _spec.transform,
                            frozen_input,
                        )
                        if inspect.isasyncgen(result):
                            await result.aclose()
                            raise ValueError("async generator result is unsupported")
                        if inspect.isgenerator(result):
                            result.close()
                            raise ValueError("generator result is unsupported")
                        if inspect.isawaitable(result):
                            if inspect.iscoroutine(result):
                                result.close()
                            raise ValueError("awaitable result is unsupported")
                        if type(result) is not WorkflowNodeResult:
                            raise ValueError("transform result is invalid")
                        if _spec.routes:
                            if result.route not in _spec.routes:
                                raise ValueError("transform route is not allowlisted")
                        elif result.route is not None:
                            raise ValueError("transform route is not allowlisted")
                        output = thaw_json(result.output)
                        _output_validator.validate(output)
                        if _workflow_json_size(output) > _MAX_WORKFLOW_TRANSFORM_BYTES:
                            raise ValueError("transform output is too large")
                        event = Event(output=output)
                        if result.route is not None:
                            event.actions.route = result.route
                        return event
                    except Exception:
                        raise ValueError(
                            "NexusKernel workflow transform failed"
                        ) from None

                retry_config = (
                    RetryConfig(max_attempts=spec.max_attempts, jitter=0.0)
                    if spec.max_attempts > 1
                    else None
                )
                node = FunctionNode(
                    func=transform_node,
                    name=spec.name,
                    rerun_on_resume=False,
                    retry_config=retry_config,
                    timeout=spec.timeout_seconds,
                    auth_config=None,
                    parameter_binding="state",
                )
            else:
                action = action_specs[spec.action_code]

                def request_input_node(
                    node_input: Any,
                    *,
                    _action: Any = action,
                    _spec: WorkflowRequestInputNodeSpec = spec,
                ) -> Any:
                    del node_input
                    return RequestInput(
                        payload={"code": _spec.action_code},
                        message=None,
                        response_schema=thaw_json(_action.response_schema),
                    )

                node = FunctionNode(
                    func=request_input_node,
                    name=spec.name,
                    rerun_on_resume=False,
                    retry_config=None,
                    timeout=None,
                    auth_config=None,
                    parameter_binding="state",
                )
            nodes[spec.name] = node
            specs_by_id[id(node)] = _ManagedWorkflowFunctionNode(
                spec=spec,
                function=node._func,
            )
        return nodes, specs_by_id

    def _build_workflow_app(
        self,
        application: AdkApplicationSpec,
        *,
        app_name: str,
        tool_executor: ToolExecutor,
    ) -> tuple[Any, str, str | None, frozenset[str]]:
        if _callable_semantic_digest(application.factory) != (
            application._factory_fingerprint
        ) or any(
            isinstance(spec, WorkflowTransformNodeSpec)
            and _callable_semantic_digest(spec.transform) != spec._semantic_fingerprint
            for spec in application.function_nodes
        ):
            raise AiProviderError("Google ADK application definition changed")

        from google.adk.apps import App, ResumabilityConfig
        from google.adk.tools import request_input  # type: ignore[attr-defined]

        adapters = {
            tool.name: self._tool_adapter(tool, tool_executor)
            for tool in tool_executor.tools
        }
        function_nodes, function_specs = self._build_workflow_function_nodes(
            application
        )
        try:
            built = application.factory(
                AdkApplicationBuildContext(
                    model=self._model,
                    tools=adapters,
                    function_nodes=function_nodes,
                )
            )
        except Exception:
            raise AiProviderError("Google ADK application factory failed") from None
        if not isinstance(built, App) or built.root_agent is None:
            raise AiProviderError("Google ADK application factory returned invalid App")
        if (
            built.plugins
            or built.events_compaction_config is not None
            or built.context_cache_config is not None
            or built.resumability_config is not None
        ):
            raise AiProviderError("Google ADK application configuration is unsupported")

        root = built.root_agent
        adapter_names = {id(adapter): name for name, adapter in adapters.items()}
        request_input_id = id(request_input) if application.resumable else None
        if application.kind == "graph":
            root, topology, terminal_node_name = self._validate_graph_application(
                root,
                adapter_names=adapter_names,
                function_specs=function_specs,
            )
            agent_control_names: frozenset[str] = frozenset()
        else:
            terminal_node_name = None
            topology, agent_control_names = self._validate_team_application(
                root,
                adapter_names=adapter_names,
                request_input_id=request_input_id,
            )

        topology_digest = hashlib.sha256(
            json.dumps(
                topology,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        topology_key = (application.agent_id, application.fingerprint)
        previous = self._application_topologies.setdefault(
            topology_key, topology_digest
        )
        if previous != topology_digest:
            raise AiProviderError("Google ADK application topology changed")

        resumability = (
            ResumabilityConfig(is_resumable=True) if application.resumable else None
        )
        normalized = App(
            name=app_name,
            root_agent=root,
            resumability_config=resumability,
        )
        return normalized, root.name, terminal_node_name, agent_control_names

    def _validate_graph_application(
        self,
        root: Any,
        *,
        adapter_names: Mapping[int, str],
        function_specs: Mapping[int, _ManagedWorkflowFunctionNode] | None = None,
    ) -> tuple[Any, dict[str, Any], str]:
        from google.adk.agents import LlmAgent
        from google.adk.workflow import FunctionNode, JoinNode, START, Workflow

        if not isinstance(root, Workflow) or root.graph is None:
            raise AiProviderError("Google ADK graph application root is invalid")
        concurrency = root.max_concurrency
        if concurrency is None:
            root = root.model_copy(
                update={"max_concurrency": self._max_workflow_concurrency}
            )
        elif (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
            or concurrency > self._max_workflow_concurrency
        ):
            raise AiProviderError("Google ADK workflow concurrency is invalid")
        terminal_node_name = self._validate_graph_shape(root.graph, start=START)
        managed_specs = function_specs or {}
        used_function_nodes: set[int] = set()
        nodes: list[tuple[Any, ...]] = []
        for node in root.graph.nodes:
            if node is START:
                nodes.append(("start", node.name))
            elif isinstance(node, LlmAgent):
                if node.mode != "single_turn" or node.sub_agents:
                    raise AiProviderError("Google ADK graph agent topology is invalid")
                nodes.append(
                    self._validate_workflow_agent(
                        node,
                        adapter_names=adapter_names,
                        request_input_id=None,
                    )
                )
            elif isinstance(node, FunctionNode):
                managed = managed_specs.get(id(node))
                if managed is None:
                    raise AiProviderError("Google ADK FunctionNode is not allowlisted")
                self._validate_managed_function_node(node, managed)
                used_function_nodes.add(id(node))
                nodes.append(
                    (
                        "function",
                        managed.spec.name,
                        json.dumps(
                            _workflow_function_node_material(managed.spec),
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        self._node_execution_topology(node),
                    )
                )
            elif isinstance(node, JoinNode):
                nodes.append(
                    (
                        "join",
                        node.name,
                        self._node_execution_topology(node),
                    )
                )
            else:
                raise AiProviderError("Google ADK graph node type is unsupported")
        if used_function_nodes != set(managed_specs):
            raise AiProviderError(
                "Google ADK managed FunctionNodes must all be used by the graph"
            )
        self._validate_managed_function_routes(root.graph, managed_specs)
        topology = {
            "kind": "graph",
            "root": root.name,
            "terminal": terminal_node_name,
            "nodes": sorted(nodes),
            "edges": sorted(
                (
                    edge.from_node.name,
                    edge.to_node.name,
                    json.dumps(edge.route, sort_keys=True, separators=(",", ":")),
                )
                for edge in root.graph.edges
            ),
            "max_concurrency": root.max_concurrency,
            "execution": self._node_execution_topology(root),
        }
        return root, topology, terminal_node_name

    @staticmethod
    def _validate_graph_shape(graph: Any, *, start: Any) -> str:
        names = {node.name for node in graph.nodes}
        outgoing: dict[str, set[str]] = {name: set() for name in names}
        indegree = {name: 0 for name in names}
        for edge in graph.edges:
            source = edge.from_node.name
            target = edge.to_node.name
            if target not in outgoing[source]:
                outgoing[source].add(target)
                indegree[target] += 1
        ready = [name for name, count in indegree.items() if count == 0]
        visited = 0
        while ready:
            name = ready.pop()
            visited += 1
            for target in outgoing[name]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if visited != len(names):
            raise AiProviderError("Google ADK workflow graph must be acyclic")
        reachable = {start.name}
        pending = [start.name]
        while pending:
            source = pending.pop()
            for target in outgoing[source]:
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)
        if reachable != names:
            raise AiProviderError(
                "Google ADK workflow graph contains unreachable nodes"
            )
        terminals = {
            name
            for name, targets in outgoing.items()
            if not targets and name != start.name
        }
        if len(terminals) != 1:
            raise AiProviderError(
                "Google ADK workflow graph must have one terminal node"
            )
        return next(iter(terminals))

    @staticmethod
    def _validate_managed_function_node(
        node: Any,
        managed: _ManagedWorkflowFunctionNode,
    ) -> None:
        spec = managed.spec
        if (
            node.name != spec.name
            or getattr(node, "_func", None) is not managed.function
            or node.parameter_binding != "state"
            or node.auth_config is not None
            or node.rerun_on_resume is not False
            or node.wait_for_output is not False
            or node.input_schema is not None
            or node.output_schema is not None
            or node.state_schema is not None
        ):
            raise AiProviderError("Google ADK managed FunctionNode was modified")
        if isinstance(spec, WorkflowTransformNodeSpec):
            if node.timeout != spec.timeout_seconds:
                raise AiProviderError("Google ADK managed FunctionNode was modified")
            if spec.max_attempts == 1:
                valid_retry = node.retry_config is None
            else:
                retry = node.retry_config
                valid_retry = (
                    retry is not None
                    and retry.max_attempts == spec.max_attempts
                    and retry.jitter == 0.0
                    and retry.initial_delay is None
                    and retry.max_delay is None
                    and retry.backoff_factor is None
                    and retry.exceptions is None
                )
            if not valid_retry:
                raise AiProviderError("Google ADK managed FunctionNode was modified")
        elif node.timeout is not None or node.retry_config is not None:
            raise AiProviderError("Google ADK managed FunctionNode was modified")

    @staticmethod
    def _validate_managed_function_routes(
        graph: Any,
        function_specs: Mapping[int, _ManagedWorkflowFunctionNode],
    ) -> None:
        for node_id, managed in function_specs.items():
            spec = managed.spec
            routed: set[str] = set()
            has_unconditional = False
            for edge in graph.edges:
                if id(edge.from_node) != node_id:
                    continue
                if edge.route is None:
                    has_unconditional = True
                    continue
                routes = edge.route if isinstance(edge.route, list) else [edge.route]
                if any(not isinstance(route, str) for route in routes):
                    raise AiProviderError(
                        "Google ADK managed FunctionNode route is invalid"
                    )
                routed.update(routes)
            expected = (
                set(spec.routes)
                if isinstance(spec, WorkflowTransformNodeSpec)
                else set()
            )
            if routed != expected or (expected and has_unconditional):
                raise AiProviderError(
                    "Google ADK managed FunctionNode routes do not match allowlist"
                )

    def _validate_team_application(
        self,
        root: Any,
        *,
        adapter_names: Mapping[int, str],
        request_input_id: int | None,
    ) -> tuple[dict[str, Any], frozenset[str]]:
        from google.adk.agents import LlmAgent
        from google.adk.tools.agent_tool import AgentTool

        if not isinstance(root, LlmAgent) or root.mode != "chat" or not root.sub_agents:
            raise AiProviderError("Google ADK team application root is invalid")
        child_by_id = {id(child): child for child in root.sub_agents}
        child_names = {child.name for child in root.sub_agents}
        if set(adapter_names.values()) & (
            child_names | {_REQUEST_INPUT_FUNCTION, "finish_task"}
        ):
            raise AiProviderError("Google ADK team tool name is reserved")
        root_controls: dict[int, str] = {}
        bound_children: set[int] = set()
        for tool in root.tools:
            if not isinstance(tool, AgentTool):
                continue
            child = child_by_id.get(id(tool.agent))
            if (
                not isinstance(child, LlmAgent)
                or tool.name != child.name
                or id(child) in bound_children
            ):
                raise AiProviderError("Google ADK team delegation tool is invalid")
            bound_children.add(id(child))
            root_controls[id(tool)] = f"delegate:{child.mode}:{child.name}"
        if bound_children != set(child_by_id):
            raise AiProviderError("Google ADK team delegation tools are incomplete")
        agents = [
            self._validate_workflow_agent(
                root,
                adapter_names=adapter_names,
                request_input_id=request_input_id,
                additional_tools=root_controls,
            )
        ]
        probe = LlmAgent(
            name="nexus_kernel_task_probe",
            model=self._model,
            mode="task",
        )
        finish_tool_types = {type(tool) for tool in probe.tools}
        for child in root.sub_agents:
            if (
                not isinstance(child, LlmAgent)
                or child.mode not in {"task", "single_turn"}
                or child.sub_agents
            ):
                raise AiProviderError("Google ADK team topology is invalid")
            child_controls: dict[int, str] = {}
            if child.mode == "task":
                finish_tools = [
                    tool for tool in child.tools if type(tool) in finish_tool_types
                ]
                finish_tool = cast(Any, finish_tools[0]) if finish_tools else None
                if (
                    len(finish_tools) != 1
                    or finish_tool is None
                    or finish_tool.name != "finish_task"
                ):
                    raise AiProviderError("Google ADK task finish tool is invalid")
                child_controls[id(finish_tool)] = f"finish:{child.name}"
            agents.append(
                self._validate_workflow_agent(
                    child,
                    adapter_names=adapter_names,
                    request_input_id=request_input_id,
                    additional_tools=child_controls,
                )
            )
        return (
            {
                "kind": "team",
                "root": root.name,
                "agents": agents,
                "execution": self._node_execution_topology(root),
            },
            frozenset(child_names),
        )

    def _validate_workflow_agent(
        self,
        agent: Any,
        *,
        adapter_names: Mapping[int, str],
        request_input_id: int | None,
        additional_tools: Mapping[int, str] | None = None,
    ) -> tuple[str, str, str, tuple[str, ...], str, str]:
        if agent.model is not self._model:
            raise AiProviderError("Google ADK application model is invalid")
        if getattr(agent, "parallel_worker", None) not in {None, False}:
            raise AiProviderError("Google ADK parallel agent execution is unsupported")
        behavior_digest = self._workflow_agent_behavior_digest(agent)
        names: list[str] = []
        allowed_controls = additional_tools or {}
        for tool in agent.tools:
            name = adapter_names.get(id(tool))
            if name is not None:
                names.append(name)
            elif request_input_id is not None and id(tool) == request_input_id:
                names.append(_REQUEST_INPUT_FUNCTION)
            elif id(tool) in allowed_controls:
                names.append(allowed_controls[id(tool)])
            else:
                raise AiProviderError("Google ADK application tool is not allowlisted")
        return (
            "agent",
            agent.name,
            agent.mode,
            tuple(sorted(names)),
            self._node_execution_topology(agent),
            behavior_digest,
        )

    @staticmethod
    def _workflow_agent_behavior_digest(agent: Any) -> str:
        if not isinstance(agent.instruction, str) or not isinstance(
            agent.global_instruction, str
        ):
            raise AiProviderError("Google ADK callable instructions are unsupported")
        if any(
            getattr(agent, field, None) is not None
            for field in _WORKFLOW_CALLBACK_FIELDS
        ):
            raise AiProviderError("Google ADK application callbacks are unsupported")
        if agent.planner is not None or agent.code_executor is not None:
            raise AiProviderError(
                "Google ADK planner and code executor are unsupported"
            )
        generate_content_config = _canonical_workflow_value(
            agent.generate_content_config
        )
        if isinstance(generate_content_config, dict) and generate_content_config.get(
            "tools"
        ):
            raise AiProviderError("Google ADK configured tools are unsupported")
        return _workflow_configuration_digest(
            {
                "description": agent.description,
                "disallow_transfer_to_parent": agent.disallow_transfer_to_parent,
                "disallow_transfer_to_peers": agent.disallow_transfer_to_peers,
                "generate_content_config": generate_content_config,
                "global_instruction": agent.global_instruction,
                "include_contents": agent.include_contents,
                "input_schema": _canonical_workflow_schema(agent.input_schema),
                "instruction": agent.instruction,
                "output_key": agent.output_key,
                "output_schema": _canonical_workflow_schema(agent.output_schema),
                "state_schema": _canonical_workflow_schema(agent.state_schema),
                "static_instruction": agent.static_instruction,
            }
        )

    @staticmethod
    def _node_execution_topology(node: Any) -> str:
        retry = (
            node.retry_config.model_dump(mode="json")
            if node.retry_config is not None
            else None
        )
        return json.dumps(
            {
                "rerun_on_resume": node.rerun_on_resume,
                "retry": retry,
                "timeout": node.timeout,
                "wait_for_output": node.wait_for_output,
                "parallel_worker": getattr(node, "parallel_worker", None),
                "public_contract": _workflow_configuration_digest(
                    {
                        "description": node.description,
                        "input_schema": _canonical_workflow_schema(node.input_schema),
                        "output_schema": _canonical_workflow_schema(node.output_schema),
                        "state_schema": _canonical_workflow_schema(node.state_schema),
                    }
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _reject_unsupported_workflow_actions(
        event: Any,
        *,
        agent_control_names: frozenset[str] = frozenset(),
    ) -> None:
        actions = getattr(event, "actions", None)
        if actions is not None and (
            actions.requested_auth_configs or actions.requested_tool_confirmations
        ):
            raise AiWorkflowActionUnsupportedError(
                "Google ADK workflow auth is unsupported"
            )
        long_running_ids = set(getattr(event, "long_running_tool_ids", None) or ())
        function_calls = event.get_function_calls()
        delegated = [
            call for call in function_calls if call.name in agent_control_names
        ]
        if len(delegated) > 1:
            raise AiWorkflowActionUnsupportedError(
                "Google ADK parallel task delegation is unsupported"
            )
        for function_call in function_calls:
            name = function_call.name
            if function_call.id in long_running_ids and name != _REQUEST_INPUT_FUNCTION:
                raise AiWorkflowActionUnsupportedError(
                    "Google ADK returned unsupported actions"
                )

    @staticmethod
    def _pending_actions_from_event(
        application: AdkApplicationSpec, event: Any
    ) -> tuple[WorkflowPendingAction, ...]:
        specs = {spec.code: spec for spec in application.action_specs}
        long_running_ids = set(getattr(event, "long_running_tool_ids", None) or ())
        pending: list[WorkflowPendingAction] = []
        for function_call in event.get_function_calls():
            if function_call.name != _REQUEST_INPUT_FUNCTION:
                continue
            runtime_id = _runtime_identifier(function_call.id)
            if runtime_id not in long_running_ids or not application.resumable:
                raise AiWorkflowActionUnsupportedError(
                    "Google ADK returned unsupported actions"
                )
            args = function_call.args
            if not isinstance(args, dict):
                raise AiWorkflowActionUnsupportedError(
                    "Google ADK returned an invalid action"
                )
            payload = args.get("payload")
            response_schema = args.get("response_schema")
            if payload is None:
                message = args.get("message")
                if not isinstance(message, str):
                    raise AiWorkflowActionUnsupportedError(
                        "Google ADK returned an invalid action"
                    )
                spec = specs.get(message)
            else:
                if not isinstance(payload, dict) or set(payload) != {"code"}:
                    raise AiWorkflowActionUnsupportedError(
                        "Google ADK returned an invalid action"
                    )
                code = payload.get("code")
                spec = specs.get(code) if isinstance(code, str) else None
            if spec is None or response_schema != thaw_json(spec.response_schema):
                raise AiWorkflowActionUnsupportedError(
                    "Google ADK returned an unknown action"
                )
            pending.append(
                WorkflowPendingAction(
                    runtime_action_id=runtime_id,
                    code=spec.code,
                    kind=spec.kind,
                )
            )
        return tuple(pending)

    def _pending_actions_for_resume(
        self,
        application: AdkApplicationSpec,
        session: Any,
        *,
        invocation_id: str,
    ) -> dict[str, WorkflowPendingAction]:
        if not application.resumable:
            raise AiProviderError("Google ADK workflow is not resumable")
        pending: dict[str, WorkflowPendingAction] = {}
        answered: set[str] = set()
        for event in session.events:
            if event.invocation_id != invocation_id:
                continue
            for action in self._pending_actions_from_event(application, event):
                if action.runtime_action_id in pending:
                    raise AiProviderError("Google ADK returned duplicate actions")
                pending[action.runtime_action_id] = action
            for response in event.get_function_responses():
                if response.name != _REQUEST_INPUT_FUNCTION:
                    continue
                runtime_id = _runtime_identifier(response.id)
                if runtime_id in answered:
                    raise AiProviderError("Google ADK returned duplicate responses")
                answered.add(runtime_id)
        if not answered.issubset(pending):
            raise AiProviderError("Google ADK workflow resume state is invalid")
        for runtime_id in answered:
            pending.pop(runtime_id)
        if not pending:
            raise AiProviderError("Google ADK workflow resume token is invalid")
        return pending

    def _workflow_output_from_event(
        self,
        event: Any,
        *,
        root_name: str,
        terminal_node_name: str | None,
    ) -> _WorkflowOutputCandidate | None:
        if event.author != root_name:
            return None
        if terminal_node_name is not None:
            node_info = getattr(event, "node_info", None)
            if node_info is None or node_info.name != terminal_node_name:
                return None
        model_version = getattr(event, "model_version", None)
        model = (
            model_version
            if isinstance(model_version, str) and model_version.strip()
            else self._model_name
        )
        event_finish_reason = event.finish_reason
        finish_reason = (
            str(getattr(event_finish_reason, "value", event_finish_reason))
            if event_finish_reason is not None
            else None
        )
        if event.output is not None:
            return _WorkflowOutputCandidate(
                value=event.output,
                text=False,
                model=model,
                finish_reason=finish_reason,
            )
        if not event.is_final_response():
            return None
        text = _public_event_text(event)
        if not text:
            return None
        return _WorkflowOutputCandidate(
            value=text,
            text=True,
            model=model,
            finish_reason=finish_reason,
        )

    async def delete_workflow_session(self, key: SessionKey, *, app_name: str) -> None:
        existing = await self._sessions.get_session(
            app_name=app_name,
            user_id=key.principal_id,
            session_id=key.session_id,
        )
        if existing is not None:
            await self._sessions.delete_session(
                app_name=app_name,
                user_id=key.principal_id,
                session_id=key.session_id,
            )

    @staticmethod
    def _tool_adapter(tool: AgentToolSpec, executor: ToolExecutor) -> Any:
        from google.adk.tools.base_tool import BaseTool
        from google.genai import types

        class RegisteredServerTool(BaseTool):
            def __init__(self) -> None:
                super().__init__(name=tool.name, description=tool.description)

            def _get_declaration(self) -> Any:
                return types.FunctionDeclaration.model_validate(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters_json_schema": thaw_json(tool.parameters),
                    }
                )

            async def run_async(
                self, *, args: dict[str, Any], tool_context: Any
            ) -> Any:
                return await executor.execute(tool.name, args)

        return RegisteredServerTool()

    async def delete_session(self, key: SessionKey) -> None:
        existing = await self._sessions.get_session(
            app_name=self._app_name,
            user_id=key.principal_id,
            session_id=key.session_id,
        )
        if existing is not None:
            await self._sessions.delete_session(
                app_name=self._app_name,
                user_id=key.principal_id,
                session_id=key.session_id,
            )

    def _schedule_runner_cleanup(self, runner: Any) -> asyncio.Task[None]:
        key = id(runner)
        existing = self._runner_cleanups.get(key)
        if existing is not None:
            if not existing.close_task.done():
                return existing.supervisor
            if (
                not existing.close_task.cancelled()
                and existing.close_task.exception() is None
            ):
                self._runner_cleanups.pop(key, None)
                return existing.close_task
            self._runner_cleanups.pop(key, None)

        async def close_runner() -> None:
            try:
                await runner.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise AiProviderError("Google ADK Runner cleanup failed") from None

        close_task = observe_task(asyncio.create_task(close_runner()))

        async def supervise() -> None:
            done, _ = await asyncio.wait(
                {close_task}, timeout=self._runner_cleanup_timeout_seconds
            )
            if not done:
                close_task.cancel()
                raise AiTimeoutError("Google ADK Runner cleanup timed out")
            try:
                close_task.result()
            except asyncio.CancelledError:
                raise AiProviderError(
                    "Google ADK Runner cleanup was cancelled"
                ) from None
            except Exception:
                raise AiProviderError("Google ADK Runner cleanup failed") from None

        supervisor = observe_task(asyncio.create_task(supervise()))
        cleanup = _RunnerCleanup(
            runner=runner, close_task=close_task, supervisor=supervisor
        )
        self._runner_cleanups[key] = cleanup

        def remove_successful(completed: asyncio.Task[None]) -> None:
            current = self._runner_cleanups.get(key)
            if (
                current is cleanup
                and not completed.cancelled()
                and completed.exception() is None
            ):
                self._runner_cleanups.pop(key, None)

        supervisor.add_done_callback(remove_successful)
        return supervisor

    async def _settle_runner_cleanups(self) -> None:
        for cleanup in tuple(self._runner_cleanups.values()):
            task = self._schedule_runner_cleanup(cleanup.runner)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise AiProviderError(
                    "Google ADK Runner cleanup is incomplete"
                ) from None
        self._prune_runner_cleanups()
        if self._runner_cleanups:
            raise AiProviderError("Google ADK Runner cleanup is incomplete")

    def _prune_runner_cleanups(self) -> None:
        for key, cleanup in tuple(self._runner_cleanups.items()):
            if (
                cleanup.close_task.done()
                and not cleanup.close_task.cancelled()
                and cleanup.close_task.exception() is None
            ):
                self._runner_cleanups.pop(key, None)

    async def close(self) -> None:
        await self._settle_runner_cleanups()
        await self._client.aio.aclose()


class GoogleAdkRuntime:
    provider_id: Literal["google_adk"] = "google_adk"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 25.0,
        tool_timeout_seconds: float = 10.0,
        max_tool_rounds: int = 4,
        max_tool_calls: int = 16,
        max_tool_argument_bytes: int = 65_536,
        max_tool_result_bytes: int = 65_536,
        max_workflow_concurrency: int = 4,
        adk_app_name: str = "nexus_kernel",
        session_ttl_seconds: float = 1800.0,
        session_max_entries: int = 256,
        session_max_events: int = 200,
        session_cleanup_timeout_seconds: float = 2.0,
        backend: AdkBackend | None = None,
        session_store: AdkSessionStore | None = None,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if not model.strip():
            raise ValueError("model must not be empty")
        if not adk_app_name.strip():
            raise ValueError("adk_app_name must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("AI timeout must be positive")
        if tool_timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")
        if (
            max_tool_rounds < 1
            or max_tool_calls < 1
            or max_tool_argument_bytes < 1
            or max_tool_result_bytes < 1
        ):
            raise ValueError("AI tool limits must be positive")
        if (
            isinstance(max_workflow_concurrency, bool)
            or not isinstance(max_workflow_concurrency, int)
            or not 1 <= max_workflow_concurrency <= 4
        ):
            raise ValueError("workflow concurrency must be between 1 and 4")
        if session_cleanup_timeout_seconds <= 0:
            raise ValueError("session cleanup timeout must be positive")
        if session_cleanup_timeout_seconds > timeout_seconds:
            raise ValueError("session cleanup timeout must not exceed the AI timeout")
        self._api_key = api_key
        self._base_url = base_url
        self._adk_app_name = adk_app_name
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.tool_timeout_seconds = tool_timeout_seconds
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls = max_tool_calls
        self.max_tool_argument_bytes = max_tool_argument_bytes
        self.max_tool_result_bytes = max_tool_result_bytes
        self.max_workflow_concurrency = max_workflow_concurrency
        self.session_cleanup_timeout_seconds = session_cleanup_timeout_seconds
        self._backend: AdkBackend | None = backend
        self._start_task: asyncio.Task[AdkBackend] | None = None
        self._session_store = session_store or BoundedMemoryAdkSessionStore(
            ttl_seconds=session_ttl_seconds,
            max_entries=session_max_entries,
            max_events=session_max_events,
            time_source=time_source,
        )
        self._healthy = True
        self._close_coordinator = CloseCoordinator()
        self._call_gate = ActiveCallGate()
        self._cleanup_tasks: dict[SessionKey, asyncio.Task[None]] = {}
        self._cleanup_deadlines: dict[SessionKey, float] = {}
        self._workflow_app_names: dict[SessionKey, str] = {}
        self._pinned_workflow_sessions: set[SessionKey] = set()
        self._late_start_close_callback_installed = False
        self._backend_closed = False
        self._store_closed = False

    def _create_default_backend(self) -> AdkBackend:
        return _SdkAdkBackend(
            api_key=self._api_key,
            model=self.model,
            base_url=self._base_url,
            app_name=self._adk_app_name,
            max_workflow_concurrency=self.max_workflow_concurrency,
        )

    async def start(self) -> None:
        if self._close_coordinator.unavailable:
            raise AiProviderError("Google ADK runtime is closed")
        if self._backend is not None:
            return
        task = self._start_task
        if task is not None and task.done():
            try:
                self._backend = task.result()
            except (asyncio.CancelledError, Exception):
                task = None
                self._start_task = None
            else:
                return
        if task is None:
            task = observe_task(
                asyncio.create_task(asyncio.to_thread(self._create_default_backend))
            )
            self._start_task = task
        try:
            backend = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("Google ADK initialization failed") from None
        self._backend = backend
        if self._close_coordinator.unavailable:
            raise AiProviderError("Google ADK runtime is closed")

    async def health_check(self) -> bool:
        runner_cleanup_pending = (
            isinstance(self._backend, _SdkAdkBackend)
            and self._backend.has_unresolved_runner_cleanup
        )
        return (
            not self._close_coordinator.unavailable
            and not self._call_gate.unavailable
            and self._healthy
            and not self._has_unresolved_cleanup()
            and not runner_cleanup_pending
        )

    def _has_unresolved_cleanup(self) -> bool:
        for task in self._cleanup_tasks.values():
            if not task.done() or task.cancelled() or task.exception() is not None:
                return True
        return False

    async def run(
        self,
        request: ChatRequest,
        *,
        identity: AgentIdentity,
        session_id: str | None = None,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse:
        try:
            deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            async with asyncio.timeout_at(deadline):
                await self.start()
                try:
                    async with self._call_gate.admit():
                        return await self._run_active(
                            request,
                            identity=identity,
                            session_id=session_id,
                            tools=tools,
                        )
                except CallAdmissionError:
                    raise AiProviderError("Google ADK runtime is closed") from None
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._healthy = False
            raise AiTimeoutError("Google ADK request timed out") from None
        except AiToolError:
            raise
        except AiTimeoutError:
            raise
        except AiProviderError:
            self._healthy = False
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("Google ADK request failed") from None

    async def invoke(
        self,
        application: AdkApplicationSpec,
        invocation: WorkflowInvocation,
        *,
        identity: AgentIdentity,
        tools: Sequence[AgentToolSpec] = (),
        timeout_seconds: float | None = None,
    ) -> WorkflowInvocationResult:
        effective_timeout = (
            self.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if (
            isinstance(effective_timeout, bool)
            or not isinstance(effective_timeout, int | float)
            or not math.isfinite(effective_timeout)
            or effective_timeout <= 0
        ):
            raise ValueError("workflow timeout must be positive and finite")
        try:
            deadline = asyncio.get_running_loop().time() + effective_timeout
            async with asyncio.timeout_at(deadline):
                await self.start()
                try:
                    async with self._call_gate.admit():
                        result = await self._invoke_workflow_active(
                            application,
                            invocation,
                            identity=identity,
                            tools=tools,
                        )
                except CallAdmissionError:
                    raise AiProviderError("Google ADK runtime is closed") from None
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._healthy = False
            raise AiTimeoutError("Google ADK workflow timed out") from None
        except AiToolError:
            raise
        except AiTimeoutError:
            raise
        except AiProviderError:
            self._healthy = False
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("Google ADK workflow failed") from None
        self._healthy = True
        return result

    async def delete_session(
        self,
        application: AdkApplicationSpec,
        *,
        identity: AgentIdentity,
        session_id: str,
    ) -> None:
        try:
            deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            async with asyncio.timeout_at(deadline):
                await self.start()
                try:
                    async with self._call_gate.admit():
                        await self._delete_workflow_session_active(
                            application,
                            identity=identity,
                            session_id=session_id,
                        )
                except CallAdmissionError:
                    raise AiProviderError("Google ADK runtime is closed") from None
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._healthy = False
            raise AiTimeoutError("Google ADK session cleanup timed out") from None
        except (AiProviderError, AiTimeoutError):
            self._healthy = False
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("Google ADK session cleanup failed") from None

    async def _invoke_workflow_active(
        self,
        application: AdkApplicationSpec,
        invocation: WorkflowInvocation,
        *,
        identity: AgentIdentity,
        tools: Sequence[AgentToolSpec],
    ) -> WorkflowInvocationResult:
        by_name = {tool.name: tool for tool in tools}
        if len(by_name) != len(tools) or set(by_name) != set(application.tool_names):
            raise AiProviderError("Google ADK workflow tools do not match allowlist")
        ordered_tools = tuple(by_name[name] for name in application.tool_names)
        executor = ToolExecutor(
            ordered_tools,
            identity=identity,
            timeout_seconds=self.tool_timeout_seconds,
            max_calls=self.max_tool_calls,
            max_argument_bytes=self.max_tool_argument_bytes,
            max_result_bytes=self.max_tool_result_bytes,
        )
        key = _workflow_session_key(
            application,
            identity=identity,
            session_id=invocation.session_id,
            model=self.model,
        )
        app_name = _runtime_workflow_app_name(application, model=self.model)
        self._workflow_app_names[key] = app_name
        return await self._invoke_workflow_with_session(
            application,
            invocation,
            key=key,
            app_name=app_name,
            executor=executor,
        )

    async def _invoke_workflow_with_session(
        self,
        application: AdkApplicationSpec,
        invocation: WorkflowInvocation,
        *,
        key: SessionKey,
        app_name: str,
        executor: ToolExecutor,
    ) -> WorkflowInvocationResult:
        backend = self._require_workflow_backend()
        is_pinned = key in self._pinned_workflow_sessions
        if invocation.resume_token is not None and not is_pinned:
            raise AiProviderError("Google ADK workflow resume state is unavailable")
        existing_cleanup = self._cleanup_tasks.get(key)
        if existing_cleanup is not None:
            await self._wait_for_cleanup(key, existing_cleanup, retry_failed=True)
        lease: AdkSessionLease | None = None
        try:
            async with self._session_store.lease(key) as acquired_lease:
                lease = acquired_lease
                try:
                    cleanup_tasks = [
                        (cleanup_key, self._schedule_cleanup(cleanup_key))
                        for cleanup_key in lease.cleanup_keys
                    ]
                    for cleanup_key, cleanup_task in cleanup_tasks:
                        await self._wait_for_cleanup(cleanup_key, cleanup_task)
                    await lease.reserve_cleanup()
                    if lease.reset_session:
                        reset_cleanup = self._schedule_cleanup(key)
                        await self._wait_for_cleanup(key, reset_cleanup)
                        self._workflow_app_names[key] = app_name
                    lease.mark_touched()
                    result = await backend.invoke_workflow(
                        application=application,
                        invocation=invocation,
                        app_name=app_name,
                        user_id=key.principal_id,
                        session_id=key.session_id,
                        tool_executor=executor,
                        max_llm_calls=self.max_tool_rounds + 1,
                    )
                    lease.record_events(result.event_count)
                    if lease.invalidated:
                        await self._unpin_workflow_session(key)
                        raise AiProviderError(
                            "Google ADK workflow exceeds session limits"
                        )
                    if not is_pinned:
                        await self._session_store.pin(key)
                        self._pinned_workflow_sessions.add(key)
                except BaseException:
                    await self._unpin_workflow_session(key)
                    if lease.touched:
                        lease.invalidate()
                    raise
        except BaseException:
            if lease is not None and lease.requires_cleanup:
                cleanup_task = self._schedule_cleanup(key)
                await self._wait_for_cleanup(key, cleanup_task)
            raise
        if lease is not None and lease.requires_cleanup:
            cleanup_task = self._schedule_cleanup(key)
            await self._wait_for_cleanup(key, cleanup_task)
        return result

    async def _delete_workflow_session_active(
        self,
        application: AdkApplicationSpec,
        *,
        identity: AgentIdentity,
        session_id: str,
    ) -> None:
        key = _workflow_session_key(
            application,
            identity=identity,
            session_id=session_id,
            model=self.model,
        )
        self._workflow_app_names[key] = _runtime_workflow_app_name(
            application,
            model=self.model,
        )
        existing_cleanup = self._cleanup_tasks.get(key)
        if existing_cleanup is not None:
            await self._wait_for_cleanup(key, existing_cleanup, retry_failed=True)
        lease: AdkSessionLease | None = None
        try:
            async with self._session_store.lease(key) as acquired_lease:
                lease = acquired_lease
                cleanup_tasks = [
                    (cleanup_key, self._schedule_cleanup(cleanup_key))
                    for cleanup_key in lease.cleanup_keys
                ]
                for cleanup_key, cleanup_task in cleanup_tasks:
                    await self._wait_for_cleanup(cleanup_key, cleanup_task)
                await self._unpin_workflow_session(key)
                await lease.reserve_cleanup()
                lease.mark_touched()
                lease.invalidate()
        except BaseException:
            if lease is not None and lease.requires_cleanup:
                self._schedule_cleanup(key)
            raise
        if lease is not None and lease.requires_cleanup:
            cleanup_task = self._schedule_cleanup(key)
            await self._wait_for_cleanup(key, cleanup_task)

    def _require_workflow_backend(self) -> _WorkflowAdkBackend:
        backend = self._backend
        if (
            backend is None
            or not callable(getattr(backend, "invoke_workflow", None))
            or not callable(getattr(backend, "delete_workflow_session", None))
        ):
            raise AiProviderError("Google ADK workflow backend is unavailable")
        return cast(_WorkflowAdkBackend, backend)

    async def _unpin_workflow_session(self, key: SessionKey) -> None:
        if key not in self._pinned_workflow_sessions:
            return
        await self._session_store.unpin(key)
        self._pinned_workflow_sessions.discard(key)

    async def _run_active(
        self,
        request: ChatRequest,
        *,
        identity: AgentIdentity,
        session_id: str | None,
        tools: Sequence[AgentToolSpec],
    ) -> ChatResponse:
        temporary = session_id is None
        key = SessionKey(identity.principal_id, session_id or uuid.uuid4().hex)
        executor = ToolExecutor(
            tools,
            identity=identity,
            timeout_seconds=self.tool_timeout_seconds,
            max_calls=self.max_tool_calls,
            max_argument_bytes=self.max_tool_argument_bytes,
            max_result_bytes=self.max_tool_result_bytes,
        )
        try:
            response = await self._run_with_session(
                request, key=key, temporary=temporary, executor=executor
            )
        except asyncio.CancelledError:
            raise
        except AiToolError:
            raise
        except AiTimeoutError:
            raise
        except AiProviderError:
            self._healthy = False
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("Google ADK request failed") from None
        self._healthy = True
        return response

    async def _run_with_session(
        self,
        request: ChatRequest,
        *,
        key: SessionKey,
        temporary: bool,
        executor: ToolExecutor,
    ) -> ChatResponse:
        system_instruction, history, new_message = self._split_messages(request)
        lease: AdkSessionLease | None = None
        backend = self._backend
        if backend is None:
            raise AiProviderError("Google ADK backend is unavailable")
        existing_cleanup = self._cleanup_tasks.get(key)
        if existing_cleanup is not None:
            await self._wait_for_cleanup(key, existing_cleanup, retry_failed=True)
        try:
            async with self._session_store.lease(
                key, temporary=temporary
            ) as acquired_lease:
                lease = acquired_lease
                try:
                    cleanup_tasks = [
                        (cleanup_key, self._schedule_cleanup(cleanup_key))
                        for cleanup_key in lease.cleanup_keys
                    ]
                    for cleanup_key, cleanup_task in cleanup_tasks:
                        await self._wait_for_cleanup(cleanup_key, cleanup_task)
                    await lease.reserve_cleanup()
                    if lease.reset_session:
                        reset_cleanup = self._schedule_cleanup(key)
                        await self._wait_for_cleanup(key, reset_cleanup)
                    lease.mark_touched()
                    result = await backend.run(
                        system_instruction=system_instruction,
                        history=history,
                        new_message=new_message,
                        user_id=key.principal_id,
                        session_id=key.session_id,
                        tool_executor=executor,
                        max_llm_calls=self.max_tool_rounds + 1,
                    )
                    lease.record_events(result.event_count)
                    response = self._normalize_result(result)
                except BaseException:
                    if lease.touched:
                        lease.invalidate()
                    raise
        except BaseException:
            if lease is not None and lease.requires_cleanup:
                self._schedule_cleanup(key)
            raise
        if lease is not None and lease.requires_cleanup:
            cleanup_task = self._schedule_cleanup(key)
            await self._wait_for_cleanup(key, cleanup_task)
        return response

    @staticmethod
    def _split_messages(
        request: ChatRequest,
    ) -> tuple[str, list[ChatMessage], ChatMessage]:
        prefix_length = 0
        for message in request.messages:
            if message.role != "system":
                break
            prefix_length += 1
        system_instruction = "\n\n".join(
            message.content for message in request.messages[:prefix_length]
        )
        conversation = request.messages[prefix_length:]
        return system_instruction, conversation[:-1], conversation[-1]

    def _normalize_result(self, result: AdkResult) -> ChatResponse:
        if not isinstance(result.text, str) or not result.text.strip():
            raise AiProviderError("Google ADK returned no text content")
        if not isinstance(result.model, str) or not result.model.strip():
            raise AiProviderError("Google ADK returned an invalid model")
        values = (result.input_tokens, result.output_tokens, result.total_tokens)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise AiProviderError("Google ADK returned invalid token usage")
        if result.total_tokens < result.input_tokens + result.output_tokens:
            raise AiProviderError("Google ADK returned inconsistent token usage")
        if result.finish_reason is not None and not isinstance(
            result.finish_reason, str
        ):
            raise AiProviderError("Google ADK returned an invalid finish reason")
        return ChatResponse(
            message=ChatMessage(role="assistant", content=result.text),
            usage=TokenUsage(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
            ),
            model=result.model,
            provider_id=self.provider_id,
            finish_reason=result.finish_reason,
        )

    def _schedule_cleanup(self, key: SessionKey) -> asyncio.Task[None]:
        existing = self._cleanup_tasks.get(key)
        if existing is not None:
            if not existing.done():
                return existing
            if not existing.cancelled() and existing.exception() is None:
                self._cleanup_tasks.pop(key, None)
                self._cleanup_deadlines.pop(key, None)
        backend = self._backend
        if backend is None:
            raise AiProviderError("Google ADK backend is unavailable")
        deadline = (
            asyncio.get_running_loop().time() + self.session_cleanup_timeout_seconds
        )

        async def cleanup() -> None:
            try:
                async with asyncio.timeout_at(deadline):
                    try:
                        app_name = self._workflow_app_names.get(key)
                        if app_name is None:
                            await backend.delete_session(key)
                        else:
                            workflow_backend = self._require_workflow_backend()
                            await workflow_backend.delete_workflow_session(
                                key, app_name=app_name
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise AiProviderError(
                            "Google ADK session cleanup failed"
                        ) from None
                    try:
                        await self._session_store.complete_cleanup(key)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise AiProviderError(
                            "Google ADK session metadata cleanup failed"
                        ) from None
                    self._workflow_app_names.pop(key, None)
            except TimeoutError:
                raise AiTimeoutError("Google ADK session cleanup timed out") from None

        task = observe_task(asyncio.create_task(cleanup()))
        self._cleanup_tasks[key] = task
        self._cleanup_deadlines[key] = deadline

        def remove_successful(completed: asyncio.Task[None]) -> None:
            if (
                self._cleanup_tasks.get(key) is completed
                and not completed.cancelled()
                and completed.exception() is None
            ):
                self._cleanup_tasks.pop(key, None)
                self._cleanup_deadlines.pop(key, None)

        task.add_done_callback(remove_successful)
        return task

    async def _wait_for_cleanup(
        self,
        key: SessionKey,
        task: asyncio.Task[None],
        *,
        retry_failed: bool = False,
    ) -> None:
        can_retry = retry_failed
        while True:
            resumed = self._resume_cleanup(key, task)
            if resumed is not task:
                task = resumed
                can_retry = False
            deadline = self._cleanup_deadlines.get(
                key,
                asyncio.get_running_loop().time()
                + self.session_cleanup_timeout_seconds,
            )
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                await asyncio.sleep(0)
                if task.done() and can_retry:
                    continue
                self._healthy = False
                raise AiTimeoutError("Google ADK session cleanup timed out") from None
            try:
                task.result()
            except asyncio.CancelledError:
                if can_retry:
                    task = self._schedule_cleanup(key)
                    can_retry = False
                    continue
                self._healthy = False
                raise AiProviderError(
                    "Google ADK session cleanup was cancelled"
                ) from None
            except (AiProviderError, AiTimeoutError):
                if can_retry:
                    task = self._schedule_cleanup(key)
                    can_retry = False
                    continue
                self._healthy = False
                raise
            except Exception:
                if can_retry:
                    task = self._schedule_cleanup(key)
                    can_retry = False
                    continue
                self._healthy = False
                raise AiProviderError("Google ADK session cleanup failed") from None
            return

    def _resume_cleanup(
        self, key: SessionKey, task: asyncio.Task[None]
    ) -> asyncio.Task[None]:
        if task.done() and (task.cancelled() or task.exception() is not None):
            return self._schedule_cleanup(key)
        return task

    def _schedule_late_start_close(self, start_task: asyncio.Task[AdkBackend]) -> None:
        if self._late_start_close_callback_installed:
            return
        self._late_start_close_callback_installed = True

        def close_late_backend(completed: asyncio.Task[AdkBackend]) -> None:
            del completed
            observe_task(asyncio.create_task(self.close()))

        start_task.add_done_callback(close_late_backend)

    async def _retire_pinned_workflow_sessions(self) -> None:
        for key in tuple(self._pinned_workflow_sessions):
            lease: AdkSessionLease | None = None
            async with self._session_store.lease(key) as acquired_lease:
                lease = acquired_lease
                for cleanup_key in lease.cleanup_keys:
                    cleanup_task = self._schedule_cleanup(cleanup_key)
                    await self._wait_for_cleanup(cleanup_key, cleanup_task)
                await self._unpin_workflow_session(key)
                await lease.reserve_cleanup()
                lease.mark_touched()
                lease.invalidate()
            if lease.requires_cleanup:
                self._schedule_cleanup(key)

    async def close(self) -> None:
        async def close_resources() -> None:
            await self._call_gate.drain(self.session_cleanup_timeout_seconds)
            start_task = self._start_task
            if self._backend is None and start_task is not None:
                done, pending_start = await asyncio.wait(
                    {start_task}, timeout=self.session_cleanup_timeout_seconds
                )
                if pending_start:
                    self._schedule_late_start_close(start_task)
                    raise AiTimeoutError("Google ADK initialization is still running")
                completed = next(iter(done))
                if not completed.cancelled() and completed.exception() is None:
                    self._backend = completed.result()

            await self._retire_pinned_workflow_sessions()

            cleanup_tasks = tuple(
                self._resume_cleanup(key, task)
                for key, task in tuple(self._cleanup_tasks.items())
            )
            if cleanup_tasks:
                done_cleanup, pending_cleanup = await asyncio.wait(
                    cleanup_tasks,
                    timeout=self.session_cleanup_timeout_seconds,
                )
                if pending_cleanup:
                    for cleanup_task in pending_cleanup:
                        cleanup_task.cancel()
                    raise AiTimeoutError("Google ADK session cleanup is still running")
                for cleanup_task in done_cleanup:
                    if cleanup_task.cancelled():
                        raise AiProviderError(
                            "Google ADK session cleanup was cancelled"
                        )
                    try:
                        cleanup_task.result()
                    except Exception:
                        raise AiProviderError(
                            "Google ADK session cleanup is incomplete"
                        ) from None

            if not self._backend_closed and self._backend is not None:
                await self._backend.close()
                self._backend_closed = True
            if not self._store_closed:
                await self._session_store.close()
                self._store_closed = True
            self._call_gate.mark_closed()

        try:
            await self._close_coordinator.close(close_resources)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("Google ADK close failed") from None
