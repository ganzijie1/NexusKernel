"""Declarative HTTP endpoint bindings for the example module."""

from app.modules.example.api.schemas import EchoRequest, EchoResponse
from nexus_kernel.kernel import HttpEndpointSpec


EXAMPLE_ENDPOINTS = (
    HttpEndpointSpec(
        path="/api/v1/example/echo",
        methods=("POST",),
        target_api="echo",
        request_schema=EchoRequest,
        response_schema=EchoResponse,
        tags=("example",),
    ),
)
