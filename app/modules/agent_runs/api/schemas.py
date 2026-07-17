"""Strict public request and response schemas for asynchronous Agent runs."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pydantic import JsonValue

from nexus_kernel.ai import TokenUsage


_STABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REQUIRES_ACTION = "requires_action"
    CANCELING = "canceling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


TERMINAL_RUN_STATUSES = frozenset(
    {
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELED,
        AgentRunStatus.EXPIRED,
    }
)


class AgentRunCreateRequest(_StrictModel):
    input: str = Field(min_length=1, max_length=1_048_576)
    session_action: Literal["new", "continue"] = "new"
    session_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str | None) -> str | None:
        if value is not None and _STABLE_ID.fullmatch(value) is None:
            raise ValueError("session_id must be a stable ASCII identifier")
        return value

    @model_validator(mode="after")
    def require_continuation_session(self) -> AgentRunCreateRequest:
        if self.session_action == "continue" and self.session_id is None:
            raise ValueError("session_id is required for continuation")
        return self


class AgentRunInputResponse(_StrictModel):
    type: Literal["input"]
    action_id: str = Field(min_length=1, max_length=128)
    value: JsonValue


class AgentRunConfirmationResponse(_StrictModel):
    type: Literal["confirmation"]
    action_id: str = Field(min_length=1, max_length=128)
    confirmed: bool


AgentRunActionResponse = Annotated[
    AgentRunInputResponse | AgentRunConfirmationResponse,
    Field(discriminator="type"),
]


class AgentRunResumeRequest(_StrictModel):
    version: int = Field(ge=1)
    actions: list[AgentRunActionResponse] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_unique_actions(self) -> AgentRunResumeRequest:
        identifiers = [action.action_id for action in self.actions]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("action_id values must be unique")
        return self


class AgentRunAction(_StrictModel):
    action_id: str
    code: str
    type: Literal["input", "confirmation"]
    response_format: Literal["json_schema"] = "json_schema"
    response_schema: dict[str, JsonValue] | bool
    expires_at: datetime


class AgentRunError(_StrictModel):
    code: str


class AgentRunSnapshot(_StrictModel):
    run_id: str
    agent_id: str
    agent_revision: str
    session_id: str
    status: AgentRunStatus
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    output: JsonValue = None
    usage: TokenUsage | None = None
    model: str | None = None
    finish_reason: str | None = None
    actions: tuple[AgentRunAction, ...] = ()
    error: AgentRunError | None = None


class AgentSessionDeleteResult(_StrictModel):
    agent_id: str
    session_id: str
    deleted: Literal[True] = True
