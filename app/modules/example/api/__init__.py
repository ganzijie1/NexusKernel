"""Example module public API contracts, schemas, and endpoint declarations."""

from app.modules.example.api.contracts import ExampleApi
from app.modules.example.api.endpoints import EXAMPLE_ENDPOINTS
from app.modules.example.api.schemas import EchoRequest, EchoResponse

__all__ = ["EXAMPLE_ENDPOINTS", "EchoRequest", "EchoResponse", "ExampleApi"]
