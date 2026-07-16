"""Application-owned Chat transport and error mapping."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.services import (
    AiProviderUnavailableError,
    AiRequestTimeoutError,
    AuthNotConfiguredError,
    ChatService,
    UnauthorizedError,
)
from nexus_kernel.ai import ChatRequest
from nexus_kernel.gateway import ApiError
from nexus_kernel.gateway.responses import success_response


def create_chat_router(chat_service: ChatService) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/chat", tags=["chat"])
    async def chat(request: ChatRequest, http_request: Request) -> Response:
        try:
            response = await chat_service.chat(request, http_request)
        except AuthNotConfiguredError:
            raise ApiError(
                "auth_not_configured",
                "Chat authentication is not configured.",
                503,
            ) from None
        except UnauthorizedError:
            raise ApiError("unauthorized", "Authentication is required.", 401) from None
        except AiRequestTimeoutError:
            raise ApiError("ai_timeout", "AI request timed out.", 504) from None
        except AiProviderUnavailableError:
            raise ApiError(
                "ai_provider_unavailable", "AI provider is unavailable.", 503
            ) from None
        return success_response(response)

    return router
