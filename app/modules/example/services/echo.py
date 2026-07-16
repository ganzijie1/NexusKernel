"""Echo business operation for the example module."""

from app.modules.example.api.schemas import EchoRequest, EchoResponse


class EchoService:
    async def echo(self, request: EchoRequest) -> EchoResponse:
        return EchoResponse(message=request.message, count=request.count)
