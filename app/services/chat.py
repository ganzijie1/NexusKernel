"""Template Chat service: trusted identity plus AI runtime dispatch."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

from fastapi import Request

from app.services.identity import (
    AuthNotConfiguredError,
    BearerIdentityResolver,
    TrustedIdentityResolver,
)
from nexus_kernel.ai import (
    AgentRuntime,
    AgentToolSpec,
    AiProviderError,
    AiTimeoutError,
    ChatRequest,
    ChatResponse,
    ModelClient,
)
from nexus_kernel.errors import FrameworkError
from nexus_kernel.providers import ProviderRegistry


class AiProviderUnavailableError(FrameworkError):
    """Raised when the application has no usable AI runtime."""


class AiRequestTimeoutError(FrameworkError):
    """Raised when the complete AI request deadline is exceeded."""


class ChatService(Protocol):
    async def chat(
        self, request: ChatRequest, http_request: Request
    ) -> ChatResponse: ...


class DisabledChatService:
    async def chat(self, request: ChatRequest, http_request: Request) -> ChatResponse:
        raise AuthNotConfiguredError("Chat authentication is not configured")


class AppChatService:
    def __init__(
        self,
        providers: ProviderRegistry,
        *,
        service_token: str | None = None,
        identity_resolver: TrustedIdentityResolver | None = None,
        tools: Sequence[AgentToolSpec] = (),
    ) -> None:
        if service_token is not None and identity_resolver is not None:
            raise ValueError("configure service_token or identity_resolver, not both")
        self._providers = providers
        self._identity_resolver = identity_resolver or BearerIdentityResolver(
            service_token
        )
        self._tools = tuple(tools)

    async def chat(self, request: ChatRequest, http_request: Request) -> ChatResponse:
        identity = await self._identity_resolver.resolve(http_request)
        runtime = self._providers.get("ai_runtime")
        if runtime is None:
            raise AiProviderUnavailableError("AI runtime is unavailable")
        try:
            chat = getattr(runtime, "chat", None)
            if callable(chat):
                client = cast(ModelClient, runtime)
                return await client.chat(
                    request,
                    identity=identity,
                    tools=self._tools,
                )
            run = getattr(runtime, "run", None)
            if callable(run):
                agent = cast(AgentRuntime, runtime)
                return await agent.run(
                    request,
                    identity=identity,
                    session_id=request.session_id,
                    tools=self._tools,
                )
        except AiTimeoutError:
            raise AiRequestTimeoutError("AI request timed out") from None
        except AiProviderError:
            raise AiProviderUnavailableError("AI runtime is unavailable") from None
        raise AiProviderUnavailableError("AI runtime contract is invalid")
