"""Google ADK agent runtime with trusted identities and bounded sessions."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, Literal, Protocol

from nexus_kernel.ai._closing import CloseCoordinator
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
from nexus_kernel.ai.errors import AiProviderError, AiTimeoutError, AiToolError
from nexus_kernel.ai.tool_executor import ToolExecutor


def _token_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AiProviderError("Google ADK returned invalid token usage")
    return value


@dataclass(frozen=True, slots=True)
class AdkResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str | None = None
    event_count: int = 1


class AdkBackend(Protocol):
    async def run(
        self,
        *,
        system_instruction: str,
        history: Sequence[ChatMessage],
        new_message: ChatMessage,
        user_id: str,
        session_id: str,
        reset_session: bool,
        tool_executor: ToolExecutor,
        max_llm_calls: int,
    ) -> AdkResult: ...

    async def delete_session(self, key: SessionKey) -> None: ...

    async def close(self) -> None: ...


class AdkSessionLease:
    """Metadata lease held while one ADK session is in use."""

    __slots__ = (
        "_event_count",
        "_invalidated",
        "_remaining_events",
        "evicted_keys",
        "key",
        "reset_session",
    )

    def __init__(
        self,
        key: SessionKey,
        *,
        reset_session: bool,
        evicted_keys: tuple[SessionKey, ...],
        remaining_events: int,
    ) -> None:
        self.key = key
        self.reset_session = reset_session
        self.evicted_keys = evicted_keys
        self._remaining_events = remaining_events
        self._event_count = 0
        self._invalidated = False

    def record_events(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise AiProviderError("Google ADK returned invalid event usage")
        self._event_count += count
        if self._event_count >= self._remaining_events:
            self._invalidated = True

    def invalidate(self) -> None:
        self._invalidated = True

    @property
    def invalidated(self) -> bool:
        return self._invalidated


class AdkSessionStore(Protocol):
    def lease(
        self, key: SessionKey, *, temporary: bool = False
    ) -> AsyncContextManager[AdkSessionLease]: ...

    async def invalidate(self, key: SessionKey) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _SessionEntry:
    last_used: float
    event_count: int = 0
    active: int = 0
    invalidated: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BoundedMemoryAdkSessionStore:
    """Bounded LRU metadata with independent locks for unrelated sessions."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 1800.0,
        max_entries: int = 256,
        max_events: int = 200,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("session TTL must be positive")
        if max_entries < 1:
            raise ValueError("session capacity must be positive")
        if max_events < 1:
            raise ValueError("session event limit must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._max_events = max_events
        self._time_source = time_source
        self._entries: dict[SessionKey, _SessionEntry] = {}
        self._condition = asyncio.Condition()
        self._closed = False

    @property
    def size(self) -> int:
        return len(self._entries)

    def lease(
        self, key: SessionKey, *, temporary: bool = False
    ) -> AsyncContextManager[AdkSessionLease]:
        return self._lease(key, temporary=temporary)

    @asynccontextmanager
    async def _lease(self, key: SessionKey, *, temporary: bool) -> Any:
        entry, created, evicted = await self._reserve(key)
        acquired = False
        lease: AdkSessionLease | None = None
        try:
            await entry.lock.acquire()
            acquired = True
            async with self._condition:
                now = self._time_source()
                reset = (
                    created
                    or entry.invalidated
                    or now - entry.last_used >= self._ttl_seconds
                    or entry.event_count >= self._max_events
                )
                if reset:
                    entry.event_count = 0
                    entry.invalidated = False
                lease = AdkSessionLease(
                    key,
                    reset_session=reset,
                    evicted_keys=tuple(evicted),
                    remaining_events=self._max_events - entry.event_count,
                )
            yield lease
        finally:
            async with self._condition:
                if lease is not None:
                    entry.event_count += lease._event_count
                    entry.invalidated = entry.invalidated or lease._invalidated
                entry.last_used = self._time_source()
                entry.active -= 1
                if temporary or (entry.invalidated and entry.active == 0):
                    self._entries.pop(key, None)
                if acquired:
                    entry.lock.release()
                self._condition.notify_all()

    async def _reserve(
        self, key: SessionKey
    ) -> tuple[_SessionEntry, bool, list[SessionKey]]:
        evicted: list[SessionKey] = []
        async with self._condition:
            while True:
                if self._closed:
                    raise AiProviderError("Google ADK session store is closed")
                now = self._time_source()
                evicted.extend(self._remove_expired(now, exclude=key))
                entry = self._entries.get(key)
                if entry is not None:
                    entry.active += 1
                    return entry, False, evicted
                if len(self._entries) < self._max_entries:
                    entry = _SessionEntry(last_used=now, active=1)
                    self._entries[key] = entry
                    return entry, True, evicted
                candidate = self._lru_idle_key()
                if candidate is not None:
                    self._entries.pop(candidate)
                    evicted.append(candidate)
                    continue
                await self._condition.wait()

    def _remove_expired(self, now: float, *, exclude: SessionKey) -> list[SessionKey]:
        expired = [
            key
            for key, entry in self._entries.items()
            if key != exclude
            and entry.active == 0
            and now - entry.last_used >= self._ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
        return expired

    def _lru_idle_key(self) -> SessionKey | None:
        idle = [
            (entry.last_used, key)
            for key, entry in self._entries.items()
            if entry.active == 0
        ]
        return min(idle, default=(0.0, None), key=lambda item: item[0])[1]

    async def invalidate(self, key: SessionKey) -> None:
        async with self._condition:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.invalidated = True
            entry.event_count = 0
            if entry.active == 0:
                self._entries.pop(key, None)
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._entries.clear()
            self._condition.notify_all()


class _SdkAdkBackend:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        app_name: str = "nexus_kernel",
    ) -> None:
        from google.adk.models.google_llm import Gemini
        from google.adk.sessions import InMemorySessionService
        from google.genai import Client, types

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

    async def run(
        self,
        *,
        system_instruction: str,
        history: Sequence[ChatMessage],
        new_message: ChatMessage,
        user_id: str,
        session_id: str,
        reset_session: bool,
        tool_executor: ToolExecutor,
        max_llm_calls: int,
    ) -> AdkResult:
        from google.adk.agents import LlmAgent
        from google.adk.agents.run_config import RunConfig
        from google.adk.events import Event
        from google.adk.runners import Runner
        from google.genai import types

        key = SessionKey(user_id, session_id)
        existing = await self._sessions.get_session(
            app_name=self._app_name, user_id=user_id, session_id=session_id
        )
        if reset_session and existing is not None:
            await self.delete_session(key)
            existing = None
        history_event_count = 0
        if existing is None:
            session = await self._sessions.create_session(
                app_name=self._app_name, user_id=user_id, session_id=session_id
            )
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
                history_event_count += 1

        agent = LlmAgent(
            name=self._agent_name,
            model=self._model,
            instruction=system_instruction,
            tools=[
                self._tool_adapter(tool, tool_executor) for tool in tool_executor.tools
            ],
        )
        runner = Runner(
            app_name=self._app_name,
            agent=agent,
            session_service=self._sessions,
        )
        final_text = ""
        input_tokens = output_tokens = total_tokens = 0
        event_count = history_event_count
        finish_reason: str | None = None
        try:
            events = runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text=new_message.content)]
                ),
                run_config=RunConfig(max_llm_calls=max_llm_calls),
            )
            async for event in events:
                event_count += 1
                if event.error_code:
                    raise AiProviderError("Google ADK returned an agent error")
                usage = event.usage_metadata
                if usage is not None:
                    input_tokens = max(
                        input_tokens, _token_count(usage.prompt_token_count)
                    )
                    output_tokens = max(
                        output_tokens, _token_count(usage.candidates_token_count)
                    )
                    total_tokens = max(
                        total_tokens, _token_count(usage.total_token_count)
                    )
                if event.finish_reason is not None:
                    finish_reason = str(
                        getattr(event.finish_reason, "value", event.finish_reason)
                    )
                if event.is_final_response() and event.content is not None:
                    final_text = "".join(
                        part.text or "" for part in event.content.parts or []
                    )
        finally:
            await runner.close()  # type: ignore[no-untyped-call]
        if not final_text:
            raise AiProviderError("Google ADK returned no text content")
        return AdkResult(
            text=final_text,
            model=self._model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=max(total_tokens, input_tokens + output_tokens),
            finish_reason=finish_reason,
            event_count=event_count,
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
                        "parameters": thaw_json(tool.parameters),
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

    async def close(self) -> None:
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
        max_tool_result_bytes: int = 65_536,
        adk_app_name: str = "nexus_kernel",
        session_ttl_seconds: float = 1800.0,
        session_max_entries: int = 256,
        session_max_events: int = 200,
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
        if max_tool_rounds < 1 or max_tool_calls < 1 or max_tool_result_bytes < 1:
            raise ValueError("AI tool limits must be positive")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.tool_timeout_seconds = tool_timeout_seconds
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls = max_tool_calls
        self.max_tool_result_bytes = max_tool_result_bytes
        self._backend = backend or _SdkAdkBackend(
            api_key=api_key,
            model=model,
            base_url=base_url,
            app_name=adk_app_name,
        )
        self._session_store = session_store or BoundedMemoryAdkSessionStore(
            ttl_seconds=session_ttl_seconds,
            max_entries=session_max_entries,
            max_events=session_max_events,
            time_source=time_source,
        )
        self._healthy = True
        self._close_coordinator = CloseCoordinator()
        self._backend_closed = False
        self._store_closed = False

    async def start(self) -> None:
        if self._close_coordinator.unavailable:
            raise AiProviderError("Google ADK runtime is closed")

    async def health_check(self) -> bool:
        return not self._close_coordinator.unavailable and self._healthy

    async def run(
        self,
        request: ChatRequest,
        *,
        identity: AgentIdentity,
        session_id: str | None = None,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse:
        if self._close_coordinator.unavailable:
            raise AiProviderError("Google ADK runtime is closed")
        temporary = session_id is None
        key = SessionKey(identity.principal_id, session_id or uuid.uuid4().hex)
        executor = ToolExecutor(
            tools,
            timeout_seconds=self.tool_timeout_seconds,
            max_calls=self.max_tool_calls,
            max_result_bytes=self.max_tool_result_bytes,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await self._run_with_session(
                    request, key=key, temporary=temporary, executor=executor
                )
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
        session_touched = False
        session_deleted = False
        succeeded = False
        try:
            async with self._session_store.lease(
                key, temporary=temporary
            ) as acquired_lease:
                lease = acquired_lease
                for evicted_key in lease.evicted_keys:
                    await self._delete_backend_session(evicted_key)
                session_touched = True
                result = await self._backend.run(
                    system_instruction=system_instruction,
                    history=history,
                    new_message=new_message,
                    user_id=key.principal_id,
                    session_id=key.session_id,
                    reset_session=lease.reset_session,
                    tool_executor=executor,
                    max_llm_calls=self.max_tool_rounds + 1,
                )
                lease.record_events(result.event_count)
                if lease.invalidated:
                    await self._delete_backend_session(key)
                    session_deleted = True
                response = self._normalize_result(result)
            succeeded = True
        except BaseException:
            if lease is not None:
                lease.invalidate()
            await self._invalidate_session_store(key)
            if session_touched and not session_deleted:
                await self._delete_backend_session(key, suppress_errors=True)
                session_deleted = True
            raise
        finally:
            if temporary and session_touched and not session_deleted:
                if lease is not None:
                    lease.invalidate()
                await self._invalidate_session_store(key)
                await self._delete_backend_session(key, suppress_errors=not succeeded)
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
        if result.finish_reason is not None and not isinstance(
            result.finish_reason, str
        ):
            raise AiProviderError("Google ADK returned an invalid finish reason")
        return ChatResponse(
            message=ChatMessage(role="assistant", content=result.text),
            usage=TokenUsage(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=max(
                    result.total_tokens,
                    result.input_tokens + result.output_tokens,
                ),
            ),
            model=result.model,
            provider_id=self.provider_id,
            finish_reason=result.finish_reason,
        )

    async def _delete_backend_session(
        self, key: SessionKey, *, suppress_errors: bool = False
    ) -> None:
        task = asyncio.create_task(self._backend.delete_session(key))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not suppress_errors:
                raise AiProviderError("Google ADK session cleanup failed") from None

    async def _invalidate_session_store(self, key: SessionKey) -> None:
        task = asyncio.create_task(self._session_store.invalidate(key))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def close(self) -> None:
        async def close_resources() -> None:
            if not self._backend_closed:
                await self._backend.close()
                self._backend_closed = True
            if not self._store_closed:
                await self._session_store.close()
                self._store_closed = True

        try:
            await self._close_coordinator.close(close_resources)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            raise AiProviderError("Google ADK close failed") from None
