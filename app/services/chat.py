"""Template Chat service: Bearer authentication plus runtime dispatch."""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from typing import cast

from pydantic import SecretStr

from nexuskernel.ai import (
    AgentRuntime,
    AgentToolSpec,
    AiProviderError,
    AiProviderUnavailableError,
    AuthNotConfiguredError,
    ChatRequest,
    ChatResponse,
    ModelClient,
    UnauthorizedError,
)
from nexuskernel.providers import ProviderRegistry
from nexuskernel.runtime import get_request_context


class AppChatService:
    def __init__(
        self,
        providers: ProviderRegistry,
        *,
        service_token: str | None,
        tools: Sequence[AgentToolSpec] = (),
    ) -> None:
        self._providers = providers
        self._service_token = SecretStr(service_token) if service_token else None
        self._tools = tuple(tools)

    def _authenticate(self, authorization: str | None) -> None:
        if self._service_token is None:
            raise AuthNotConfiguredError("Chat authentication is not configured")
        if authorization is None or not authorization.startswith("Bearer "):
            raise UnauthorizedError("Chat request is unauthorized")
        presented = authorization.removeprefix("Bearer ")
        expected = self._service_token.get_secret_value()
        if not presented or not hmac.compare_digest(presented, expected):
            raise UnauthorizedError("Chat request is unauthorized")

    async def chat(
        self, request: ChatRequest, authorization: str | None
    ) -> ChatResponse:
        self._authenticate(authorization)
        runtime = self._providers.get("ai_runtime")
        if runtime is None:
            raise AiProviderUnavailableError("AI runtime is unavailable")
        try:
            chat = getattr(runtime, "chat", None)
            if callable(chat):
                client = cast(ModelClient, runtime)
                return await client.chat(request, self._tools)
            run = getattr(runtime, "run", None)
            if callable(run):
                agent = cast(AgentRuntime, runtime)
                context = get_request_context()
                user_id = request.user_id or "anonymous"
                session_id = request.session_id or (
                    context.request_id if context else "default-session"
                )
                return await agent.run(
                    request,
                    user_id=user_id,
                    session_id=session_id,
                    tools=self._tools,
                )
        except AiProviderError as exc:
            raise AiProviderUnavailableError("AI runtime is unavailable") from exc
        raise AiProviderUnavailableError("AI runtime contract is invalid")
