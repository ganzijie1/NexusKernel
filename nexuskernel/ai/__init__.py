"""Provider-neutral AI contracts and concrete SDK adapters."""

from nexuskernel.ai.contracts import (
    AgentRuntime,
    AgentToolSpec,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatService,
    DisabledChatService,
    ModelClient,
    ProviderId,
    TokenUsage,
)
from nexuskernel.ai.errors import (
    AiProviderError,
    AiProviderUnavailableError,
    AiTimeoutError,
    AuthNotConfiguredError,
    UnauthorizedError,
)
from nexuskernel.ai.google_adk import AdkBackend, AdkResult, GoogleAdkRuntime
from nexuskernel.ai.litellm_client import LiteLLMClient
from nexuskernel.ai.openai_compatible import OpenAICompatibleClient

__all__ = [
    "AdkBackend",
    "AdkResult",
    "AgentRuntime",
    "AgentToolSpec",
    "AiProviderError",
    "AiProviderUnavailableError",
    "AiTimeoutError",
    "AuthNotConfiguredError",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ChatService",
    "DisabledChatService",
    "GoogleAdkRuntime",
    "LiteLLMClient",
    "ModelClient",
    "OpenAICompatibleClient",
    "ProviderId",
    "TokenUsage",
    "UnauthorizedError",
]
