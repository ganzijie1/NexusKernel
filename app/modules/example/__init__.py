"""Lazy public example module and schema exports."""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.modules.example.api.schemas import EchoRequest, EchoResponse
    from app.modules.example.module import ExampleModule

__all__ = ["EchoRequest", "EchoResponse", "ExampleModule"]

_EXPORTS = {
    "EchoRequest": ("app.modules.example.api.schemas", "EchoRequest"),
    "EchoResponse": ("app.modules.example.api.schemas", "EchoResponse"),
    "ExampleModule": ("app.modules.example.module", "ExampleModule"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_name), attribute)
    globals()[name] = value
    return value
