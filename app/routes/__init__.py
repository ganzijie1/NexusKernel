"""Application-owned HTTP route factories."""

import importlib
from typing import TYPE_CHECKING, Any

from app.routes.chat import create_chat_router

if TYPE_CHECKING:
    from app.routes.agent_runs import create_agent_runs_router

__all__ = ["create_agent_runs_router", "create_chat_router"]


def __getattr__(name: str) -> Any:
    if name == "create_agent_runs_router":
        module = importlib.import_module("app.routes.agent_runs")
        value = module.create_agent_runs_router
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
