"""Public callable contract for the example module."""

from typing import Protocol

from app.modules.example.api.schemas import EchoRequest, EchoResponse


class ExampleApi(Protocol):
    async def echo(self, request: EchoRequest) -> EchoResponse: ...
