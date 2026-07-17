"""Application-owned Agent Runs transport and stable error mapping."""

from __future__ import annotations

from typing import Any, Never

from fastapi import APIRouter, Header, Request
from starlette.responses import Response

from app.modules.agent_runs.api import (
    AgentRunCreateRequest,
    AgentRunResumeRequest,
    AgentRunSnapshot,
    AgentSessionDeleteResult,
)
from app.modules.agent_runs.api.schemas import TERMINAL_RUN_STATUSES
from app.services.agent_runs import (
    AgentRunsError,
    AgentRunsService,
)
from app.services.identity import AuthNotConfiguredError, UnauthorizedError
from nexus_kernel.gateway import ApiError, ErrorEnvelope, SuccessEnvelope
from nexus_kernel.gateway.responses import success_response


_CACHE_CONTROL_HEADER = {
    "description": "This process-local snapshot must not be cached.",
    "schema": {"type": "string", "const": "no-store"},
}
_LOCATION_HEADER = {
    "description": "Canonical path of the Agent Run snapshot.",
    "schema": {"type": "string"},
}
_RETRY_AFTER_HEADER = {
    "description": "Suggested polling delay for a non-terminal run, in seconds.",
    "schema": {"type": "integer", "minimum": 1},
}


def _success_response_contract(
    model: object, *, location: bool = False, retry_after: bool = False
) -> dict[str, Any]:
    headers: dict[str, Any] = {"Cache-Control": _CACHE_CONTROL_HEADER}
    if location:
        headers["Location"] = _LOCATION_HEADER
    if retry_after:
        headers["Retry-After"] = _RETRY_AFTER_HEADER
    return {"model": model, "headers": headers}


def _error_response_contracts(
    *status_codes: int,
) -> dict[int | str, dict[str, Any]]:
    return {
        status_code: {
            "model": ErrorEnvelope,
            "headers": {"Cache-Control": _CACHE_CONTROL_HEADER},
        }
        for status_code in status_codes
    }


def create_agent_runs_router(service: AgentRunsService) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/v1/agents/{agent_id}/runs",
        tags=["agent-runs"],
        status_code=202,
        response_model=SuccessEnvelope[AgentRunSnapshot],
        responses={
            202: _success_response_contract(
                SuccessEnvelope[AgentRunSnapshot], location=True
            ),
            **_error_response_contracts(401, 404, 409, 413, 422, 429, 500, 503),
        },
    )
    async def create_run(
        agent_id: str,
        request: AgentRunCreateRequest,
        http_request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> Response:
        try:
            snapshot = await service.create_run(
                agent_id,
                request,
                http_request,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            _map_error(exc)
        response = success_response(snapshot, status_code=202)
        response.headers["Location"] = f"/api/v1/agent-runs/{snapshot.run_id}"
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(
        "/api/v1/agent-runs/{run_id}",
        tags=["agent-runs"],
        response_model=SuccessEnvelope[AgentRunSnapshot],
        responses={
            200: _success_response_contract(
                SuccessEnvelope[AgentRunSnapshot], retry_after=True
            ),
            **_error_response_contracts(401, 404, 500, 503),
        },
    )
    async def get_run(run_id: str, http_request: Request) -> Response:
        try:
            snapshot = await service.get_run(run_id, http_request)
        except Exception as exc:
            _map_error(exc)
        response = success_response(snapshot)
        if snapshot.status not in TERMINAL_RUN_STATUSES:
            response.headers["Retry-After"] = "1"
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post(
        "/api/v1/agent-runs/{run_id}/resume",
        tags=["agent-runs"],
        status_code=202,
        response_model=SuccessEnvelope[AgentRunSnapshot],
        responses={
            202: _success_response_contract(
                SuccessEnvelope[AgentRunSnapshot], location=True
            ),
            **_error_response_contracts(401, 404, 409, 413, 422, 429, 500, 503),
        },
    )
    async def resume_run(
        run_id: str,
        request: AgentRunResumeRequest,
        http_request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> Response:
        try:
            snapshot = await service.resume_run(
                run_id,
                request,
                http_request,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            _map_error(exc)
        response = success_response(snapshot, status_code=202)
        response.headers["Location"] = f"/api/v1/agent-runs/{snapshot.run_id}"
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.delete(
        "/api/v1/agent-runs/{run_id}",
        tags=["agent-runs"],
        response_model=SuccessEnvelope[AgentRunSnapshot],
        responses={
            200: _success_response_contract(
                SuccessEnvelope[AgentRunSnapshot], location=True
            ),
            202: _success_response_contract(
                SuccessEnvelope[AgentRunSnapshot], location=True
            ),
            **_error_response_contracts(401, 404, 500, 503),
        },
    )
    async def cancel_run(run_id: str, http_request: Request) -> Response:
        try:
            snapshot = await service.cancel_run(run_id, http_request)
        except Exception as exc:
            _map_error(exc)
        status_code = 200 if snapshot.status in TERMINAL_RUN_STATUSES else 202
        response = success_response(snapshot, status_code=status_code)
        response.headers["Location"] = f"/api/v1/agent-runs/{snapshot.run_id}"
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.delete(
        "/api/v1/agents/{agent_id}/sessions/{session_id}",
        tags=["agent-runs"],
        response_model=SuccessEnvelope[AgentSessionDeleteResult],
        responses={
            200: _success_response_contract(SuccessEnvelope[AgentSessionDeleteResult]),
            **_error_response_contracts(401, 404, 409, 500, 503),
        },
    )
    async def delete_session(
        agent_id: str, session_id: str, http_request: Request
    ) -> Response:
        try:
            result = await service.delete_session(agent_id, session_id, http_request)
        except Exception as exc:
            _map_error(exc)
        response = success_response(result)
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


def _map_error(error: Exception) -> Never:
    if isinstance(error, AuthNotConfiguredError):
        raise ApiError(
            "auth_not_configured",
            "Agent Runs authentication is not configured.",
            503,
        ) from None
    if isinstance(error, UnauthorizedError):
        raise ApiError("unauthorized", "Authentication is required.", 401) from None
    if isinstance(error, AgentRunsError):
        raise ApiError(
            error.code,
            error.public_message,
            error.status_code,
        ) from None
    raise error
