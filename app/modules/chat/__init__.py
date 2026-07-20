"""Lazy public Chat module configuration and assembly exports."""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.modules.chat.module import ChatModule, ChatModuleConfig, create_ai_runtime

__all__ = ["ChatModule", "ChatModuleConfig", "create_ai_runtime"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module("app.modules.chat.module"), name)
    globals()[name] = value
    return value
