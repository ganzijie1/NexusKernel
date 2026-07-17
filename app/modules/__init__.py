"""Template-owned application lifecycle modules."""

import importlib
from typing import TYPE_CHECKING, Any

from app.modules.chat import ChatModule, ChatModuleConfig, create_ai_runtime
from app.modules.example import EchoRequest, EchoResponse, ExampleModule

if TYPE_CHECKING:
    from app.modules.agent_runs import AgentRunsModule, AgentRunsModuleConfig

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


def __getattr__(name: str) -> Any:
    if name in {"AgentRunsModule", "AgentRunsModuleConfig"}:
        module = importlib.import_module("app.modules.agent_runs")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
