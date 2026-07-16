"""Provider-neutral AI contracts and concrete SDK adapters."""

from nexus_kernel.ai.contracts import (
    AgentIdentity,
    AgentRuntime,
    AgentToolSpec,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ModelClient,
    ProviderId,
    SessionKey,
    TokenUsage,
)
from nexus_kernel.ai.errors import (
    AiProviderError,
    AiTimeoutError,
    AiToolError,
)
from nexus_kernel.ai.google_adk import (
    AdkBackend,
    AdkResult,
    AdkSessionLease,
    AdkSessionStore,
    BoundedMemoryAdkSessionStore,
    GoogleAdkRuntime,
)
from nexus_kernel.ai.litellm_client import LiteLLMClient
from nexus_kernel.ai.openai_compatible import OpenAICompatibleClient
from nexus_kernel.ai.tool_executor import ToolExecutor

__all__ = [
    "AdkBackend",
    "AdkResult",
    "AdkSessionLease",
    "AdkSessionStore",
    "AgentIdentity",
    "AgentRuntime",
    "AgentToolSpec",
    "AiProviderError",
    "AiTimeoutError",
    "AiToolError",
    "BoundedMemoryAdkSessionStore",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "GoogleAdkRuntime",
    "LiteLLMClient",
    "ModelClient",
    "OpenAICompatibleClient",
    "ProviderId",
    "SessionKey",
    "TokenUsage",
    "ToolExecutor",
]
