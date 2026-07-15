"""Small business-layer example proving the framework extension contract."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from nexuskernel.kernel import HttpEndpointSpec, Module


class EchoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    count: int = Field(default=1, ge=1, le=10)


class EchoResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    count: int


class ExampleApi(Protocol):
    async def echo(self, request: EchoRequest) -> EchoResponse: ...


class ExampleModule(Module):
    name = "example"
    api_contract = ExampleApi
    http_endpoints = (
        HttpEndpointSpec(
            path="/api/v1/example/echo",
            methods=("POST",),
            target_api="echo",
            request_schema=EchoRequest,
            response_schema=EchoResponse,
            tags=("example",),
        ),
    )

    async def echo(self, request: EchoRequest) -> EchoResponse:
        return EchoResponse(message=request.message, count=request.count)
