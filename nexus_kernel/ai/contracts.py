"""Provider-neutral non-streaming text and agent contracts."""

from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias

from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ProviderId: TypeAlias = Literal["openai_compatible", "litellm", "google_adk"]
_STABLE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChatMessage(_StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=100_000)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)


class ChatRequest(_StrictModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_session_id(value)
        return value

    @model_validator(mode="after")
    def validate_conversation(self) -> ChatRequest:
        conversation_started = False
        previous_role: str | None = None
        for message in self.messages:
            if message.role == "system":
                if conversation_started:
                    raise ValueError("system messages must form a leading prefix")
                continue
            conversation_started = True
            if message.role == "tool":
                raise ValueError("external tool history is not supported")
            if previous_role is None and message.role != "user":
                raise ValueError("conversation history must start with a user message")
            if message.role == previous_role:
                raise ValueError("user and assistant messages must alternate")
            previous_role = message.role
        if previous_role != "user":
            raise ValueError("chat request must end with a user message")
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


def _validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _STABLE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a stable ASCII identifier")


def _validate_session_id(value: str) -> None:
    _validate_identifier(value, "session_id")


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    principal_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.principal_id, "principal_id")


ToolHandler: TypeAlias = Callable[[dict[str, Any], AgentIdentity], Any | Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class SessionKey:
    principal_id: str
    session_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.principal_id, "principal_id")
        _validate_session_id(self.session_id)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Return a detached JSON-compatible copy of a frozen contract value."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [thaw_json(item) for item in value]
    return copy.deepcopy(value)


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
        if not callable(self.handler):
            raise ValueError("tool handler must be callable")
        parameters = copy.deepcopy(dict(self.parameters))
        if parameters.get("type") != "object":
            raise ValueError("tool parameters must be a JSON object schema")
        try:
            Draft202012Validator.check_schema(parameters)
        except Exception:
            raise ValueError("tool parameters must be a valid JSON Schema") from None
        object.__setattr__(self, "parameters", _freeze_json(parameters))


class ModelClient(Protocol):
    provider_id: ProviderId

    async def chat(
        self,
        request: ChatRequest,
        *,
        identity: AgentIdentity,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse: ...

    async def close(self) -> None: ...


class AgentRuntime(Protocol):
    provider_id: ProviderId

    async def run(
        self,
        request: ChatRequest,
        *,
        identity: AgentIdentity,
        session_id: str | None = None,
        tools: Sequence[AgentToolSpec] = (),
    ) -> ChatResponse: ...

    async def close(self) -> None: ...
