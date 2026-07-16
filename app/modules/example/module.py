"""Example module lifecycle and assembly."""

from app.modules.example.api import (
    EXAMPLE_ENDPOINTS,
    EchoRequest,
    EchoResponse,
    ExampleApi,
)
from app.modules.example.services import EchoService
from nexus_kernel.kernel import Module


class ExampleModule(Module):
    name = "example"
    api_contract = ExampleApi
    http_endpoints = EXAMPLE_ENDPOINTS

    def __init__(self) -> None:
        super().__init__()
        self._service = EchoService()

    async def echo(self, request: EchoRequest) -> EchoResponse:
        return await self._service.echo(request)
