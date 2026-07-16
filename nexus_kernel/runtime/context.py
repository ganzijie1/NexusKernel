"""Request and trace context propagated through async task boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    trace_id: str


_CURRENT_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "nexus_kernel_request_context", default=None
)


def _validate_identifier(value: str, field: str) -> None:
    if not value or len(value) > 128 or "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be a non-empty single-line identifier")


@contextmanager
def bind_request_context(request_id: str, trace_id: str) -> Iterator[RequestContext]:
    _validate_identifier(request_id, "request_id")
    _validate_identifier(trace_id, "trace_id")
    context = RequestContext(request_id=request_id, trace_id=trace_id)
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_CONTEXT.reset(token)


def get_request_context() -> RequestContext | None:
    return _CURRENT_CONTEXT.get()
