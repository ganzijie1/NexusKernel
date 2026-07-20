"""Lazy lifecycle boundary for process-local multi-agent runs."""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.modules.agent_runs.module import AgentRunsModule, AgentRunsModuleConfig

__all__ = ["AgentRunsModule", "AgentRunsModuleConfig"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module("app.modules.agent_runs.module"), name)
    globals()[name] = value
    return value
