"""Secret-safe HTTP response envelopes."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from starlette.responses import JSONResponse

from nexus_kernel.runtime import get_request_context


ResponseData = TypeVar("ResponseData")


class ResponseMeta(BaseModel):
    request_id: str | None = None
    trace_id: str | None = None


class SuccessEnvelope(BaseModel, Generic[ResponseData]):
    success: Literal[True]
    data: ResponseData
    meta: ResponseMeta


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    success: Literal[False]
    error: ErrorDetail
    meta: ResponseMeta


def _meta() -> dict[str, str | None]:
    context = get_request_context()
    return {
        "request_id": context.request_id if context else None,
        "trace_id": context.trace_id if context else None,
    }


def success_response(data: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder({"success": True, "data": data, "meta": _meta()}),
    )


def error_response(code: str, message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": {"code": code, "message": message},
            "meta": _meta(),
        },
        headers={"Cache-Control": "no-store"},
    )
