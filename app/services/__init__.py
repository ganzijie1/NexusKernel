"""Application-owned reusable service policies."""

import importlib
from typing import TYPE_CHECKING, Any

from app.services.chat import (
    AiProviderUnavailableError,
    AiRequestTimeoutError,
    AppChatService,
    ChatService,
    DisabledChatService,
)
from app.services.identity import (
    AuthNotConfiguredError,
    BearerIdentityResolver,
    TrustedIdentityResolver,
    UnauthorizedError,
)

if TYPE_CHECKING:
    from app.services.agent_runs import (
        AgentAuthorizer,
        AgentRunsService,
        AppAgentRunsService,
    )

__all__ = [
    "AgentAuthorizer",
    "AgentRunsService",
    "AiProviderUnavailableError",
    "AiRequestTimeoutError",
    "AppChatService",
    "AppAgentRunsService",
    "AuthNotConfiguredError",
    "ChatService",
    "DisabledChatService",
    "UnauthorizedError",
    "BearerIdentityResolver",
    "TrustedIdentityResolver",
]


def __getattr__(name: str) -> Any:
    if name in {"AgentAuthorizer", "AgentRunsService", "AppAgentRunsService"}:
        module = importlib.import_module("app.services.agent_runs")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
