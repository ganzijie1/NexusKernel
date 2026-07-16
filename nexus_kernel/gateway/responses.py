"""Secret-safe HTTP response envelopes."""

from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse

from nexus_kernel.runtime import get_request_context


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
    )
