"""Google ADK agent runtime with explicit credentials and TTL sessions."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from nexuskernel.ai.contracts import (
    AgentToolSpec,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    TokenUsage,
)
from nexuskernel.ai.errors import AiProviderError, AiTimeoutError


@dataclass(frozen=True, slots=True)
class AdkResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str | None = None


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
        tools: Sequence[AgentToolSpec],
    ) -> AdkResult: ...

    async def close(self) -> None: ...


class _SdkAdkBackend:
    def __init__(
        self, *, api_key: str, model: str, base_url: str | None = None
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
        self._app_name = "nexuskernel"
        self._agent_name = "nexuskernel_agent"

    async def run(
        self,
        *,
        system_instruction: str,
        history: Sequence[ChatMessage],
        new_message: ChatMessage,
        user_id: str,
        session_id: str,
        reset_session: bool,
        tools: Sequence[AgentToolSpec],
    ) -> AdkResult:
        from google.adk.agents import LlmAgent
        from google.adk.events import Event
        from google.adk.runners import Runner
        from google.genai import types

        existing = await self._sessions.get_session(
            app_name=self._app_name, user_id=user_id, session_id=session_id
        )
        if reset_session and existing is not None:
            await self._sessions.delete_session(
                app_name=self._app_name, user_id=user_id, session_id=session_id
            )
            existing = None
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

        agent = LlmAgent(
            name=self._agent_name,
            model=self._model,
            instruction=system_instruction,
            tools=[self._tool_adapter(tool) for tool in tools],
        )
        runner = Runner(
            app_name=self._app_name,
            agent=agent,
            session_service=self._sessions,
        )
        final_text = ""
        input_tokens = output_tokens = total_tokens = 0
        finish_reason: str | None = None
        try:
            events = runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text=new_message.content)]
                ),
            )
            async for event in events:
                if event.error_code:
                    raise AiProviderError("Google ADK returned an agent error")
                usage = event.usage_metadata
                if usage is not None:
                    input_tokens = max(input_tokens, int(usage.prompt_token_count or 0))
                    output_tokens = max(
                        output_tokens, int(usage.candidates_token_count or 0)
                    )
                    total_tokens = max(total_tokens, int(usage.total_token_count or 0))
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
        )

    @staticmethod
    def _tool_adapter(tool: AgentToolSpec) -> Any:
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
                        "parameters": dict(tool.parameters),
                    }
                )

            async def run_async(
                self, *, args: dict[str, Any], tool_context: Any
            ) -> Any:
                try:
                    result = tool.handler(args)
                    if inspect.isawaitable(result):
                        return await result
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise AiProviderError("Server tool execution failed") from None

        return RegisteredServerTool()

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
        timeout_seconds: float = 30.0,
        session_ttl_seconds: float = 1800.0,
        backend: AdkBackend | None = None,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0 or session_ttl_seconds <= 0:
            raise ValueError("AI timeouts and session TTL must be positive")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self._backend = backend or _SdkAdkBackend(
            api_key=api_key, model=model, base_url=base_url
        )
        self._time_source = time_source
        self._last_used: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise AiProviderError("Google ADK runtime is closed")

    async def health_check(self) -> bool:
        return not self._closed

    async def run(
        self,
        request: ChatRequest,
        *,
        user_id: str,
        session_id: str,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse:
        if self._closed:
            raise AiProviderError("Google ADK runtime is closed")
        last_user_index = max(
            index
            for index, message in enumerate(request.messages)
            if message.role == "user"
        )
        system_instruction = "\n\n".join(
            message.content for message in request.messages if message.role == "system"
        )
        history = [
            message
            for index, message in enumerate(request.messages)
            if index < last_user_index and message.role != "system"
        ]
        new_message = request.messages[last_user_index]
        key = (user_id, session_id)
        async with self._lock:
            now = self._time_source()
            previous = self._last_used.get(key)
            reset_session = (
                previous is None or now - previous >= self.session_ttl_seconds
            )
            try:
                result = await asyncio.wait_for(
                    self._backend.run(
                        system_instruction=system_instruction,
                        history=history,
                        new_message=new_message,
                        user_id=user_id,
                        session_id=session_id,
                        reset_session=reset_session,
                        tools=tools,
                    ),
                    timeout=self.timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                raise AiTimeoutError("Google ADK request timed out") from exc
            except AiProviderError:
                raise
            except Exception:
                raise AiProviderError("Google ADK request failed") from None
            self._last_used[key] = now

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

    async def close(self) -> None:
        if self._closed:
            return
        await self._backend.close()
        self._closed = True
        self._last_used.clear()
