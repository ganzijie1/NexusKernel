"""Provider-neutral non-streaming text and agent contracts."""

from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProviderId: TypeAlias = Literal["openai_compatible", "litellm", "google_adk"]
ToolHandler: TypeAlias = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessage(_StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=100_000)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)


class ChatRequest(_StrictModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_user_turn(self) -> ChatRequest:
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("chat request requires at least one user message")
        return self


class TokenUsage(_StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> TokenUsage:
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens cannot be smaller than normalized parts")
        return self


class ChatResponse(_StrictModel):
    message: ChatMessage
    usage: TokenUsage
    model: str
    provider_id: ProviderId
    finish_reason: str | None = None

    @model_validator(mode="after")
    def require_assistant_message(self) -> ChatResponse:
        if self.message.role != "assistant":
            raise ValueError("chat response must contain an assistant message")
        return self


@dataclass(frozen=True, slots=True)
class AgentToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: ToolHandler

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", self.name) is None:
            raise ValueError("tool name must be a stable lowercase identifier")
        if not self.description.strip():
            raise ValueError("tool description must not be empty")
        parameters = copy.deepcopy(dict(self.parameters))
        if parameters.get("type") != "object":
            raise ValueError("tool parameters must be a JSON object schema")
        object.__setattr__(self, "parameters", MappingProxyType(parameters))


class ModelClient(Protocol):
    provider_id: ProviderId

    async def chat(
        self,
        request: ChatRequest,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse: ...

    async def close(self) -> None: ...


class AgentRuntime(Protocol):
    provider_id: ProviderId

    async def run(
        self,
        request: ChatRequest,
        *,
        user_id: str,
        session_id: str,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse: ...

    async def close(self) -> None: ...


class ChatService(Protocol):
    async def chat(
        self, request: ChatRequest, authorization: str | None
    ) -> ChatResponse: ...


class DisabledChatService:
    async def chat(
        self, request: ChatRequest, authorization: str | None
    ) -> ChatResponse:
        from nexuskernel.ai.errors import AuthNotConfiguredError

        raise AuthNotConfiguredError("Chat authentication is not configured")
