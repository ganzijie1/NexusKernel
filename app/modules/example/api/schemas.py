"""Strict request and response schemas for the example module."""

from pydantic import BaseModel, ConfigDict, Field


class EchoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    count: int = Field(default=1, ge=1, le=10)


class EchoResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    count: int
