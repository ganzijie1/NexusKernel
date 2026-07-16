"""Public example module and schema exports."""

from app.modules.example.api.schemas import EchoRequest, EchoResponse
from app.modules.example.module import ExampleModule

__all__ = ["EchoRequest", "EchoResponse", "ExampleModule"]
