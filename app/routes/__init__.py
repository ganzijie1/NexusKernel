"""Application-owned HTTP route factories."""

from app.routes.chat import create_chat_router

__all__ = ["create_chat_router"]
