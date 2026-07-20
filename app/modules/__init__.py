"""Lazy public exports for template-owned application lifecycle modules."""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.modules.agent_runs.module import AgentRunsModule, AgentRunsModuleConfig
    from app.modules.chat.module import ChatModule, ChatModuleConfig, create_ai_runtime
    from app.modules.example.api.schemas import EchoRequest, EchoResponse
    from app.modules.example.module import ExampleModule

__all__ = [
    "AgentRunsModule",
    "AgentRunsModuleConfig",
    "ChatModule",
    "ChatModuleConfig",
    "EchoRequest",
    "EchoResponse",
    "ExampleModule",
    "create_ai_runtime",
]

_EXPORTS = {
    "AgentRunsModule": ("app.modules.agent_runs.module", "AgentRunsModule"),
    "AgentRunsModuleConfig": (
        "app.modules.agent_runs.module",
        "AgentRunsModuleConfig",
    ),
    "ChatModule": ("app.modules.chat.module", "ChatModule"),
    "ChatModuleConfig": ("app.modules.chat.module", "ChatModuleConfig"),
    "EchoRequest": ("app.modules.example.api.schemas", "EchoRequest"),
    "EchoResponse": ("app.modules.example.api.schemas", "EchoResponse"),
    "ExampleModule": ("app.modules.example.module", "ExampleModule"),
    "create_ai_runtime": ("app.modules.chat.module", "create_ai_runtime"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_name), attribute)
    globals()[name] = value
    return value
