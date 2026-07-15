"""Gateway limits, context propagation, timeouts, and request metrics."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nexuskernel.gateway.responses import error_response
from nexuskernel.observability import MetricsRegistry
from nexuskernel.runtime import bind_request_context


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TRACEPARENT = re.compile(
    r"[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}"
)


def _valid_id(value: str | None) -> bool:
    return value is not None and _ID.fullmatch(value) is not None


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        supplied_request_id = request.headers.get("x-request-id")
        supplied_trace_id = request.headers.get("x-trace-id")
        if supplied_request_id is not None and not _valid_id(supplied_request_id):
            return self._invalid_identifier_response(
                "invalid_request_id", "Request ID is invalid."
            )
        if supplied_trace_id is not None and not _valid_id(supplied_trace_id):
            return self._invalid_identifier_response(
                "invalid_trace_id", "Trace ID is invalid."
            )

        request_id = supplied_request_id or uuid.uuid4().hex
        traceparent = request.headers.get("traceparent", "").lower()
        trace_match = _TRACEPARENT.fullmatch(traceparent)
        trace_id = supplied_trace_id or (
            trace_match.group("trace") if trace_match else uuid.uuid4().hex
        )
        with bind_request_context(request_id, trace_id):
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            response.headers["x-trace-id"] = trace_id
            return response

    @staticmethod
    def _invalid_identifier_response(code: str, message: str) -> Response:
        request_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        with bind_request_context(request_id, trace_id):
            response = error_response(code, message, status_code=400)
        response.headers["x-request-id"] = request_id
        response.headers["x-trace-id"] = trace_id
        return response


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, limit: int) -> None:
        self.app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        raw_length = headers.get("content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    "invalid_content_length",
                    "Content-Length is invalid.",
                    400,
                )
                return
            if content_length < 0:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    "invalid_content_length",
                    "Content-Length is invalid.",
                    400,
                )
                return
            if content_length > self._limit:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    "request_body_too_large",
                    "Request body exceeds the configured limit.",
                    413,
                )
                return

        messages: deque[Message] = deque()
        received = 0
        more_body = True
        while more_body:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            received += len(message.get("body", b""))
            if received > self._limit:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    "request_body_too_large",
                    "Request body exceeds the configured limit.",
                    413,
                )
                return
            more_body = message.get("more_body", False)

        async def replay_receive() -> Message:
            if messages:
                return messages.popleft()
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _send_error(
        scope: Scope,
        receive: Receive,
        send: Send,
        code: str,
        message: str,
        status_code: int,
    ) -> None:
        response = error_response(code, message, status_code=status_code)
        await response(scope, receive, send)


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: Callable[..., Awaitable[None]], *, timeout_seconds: float
    ) -> None:
        super().__init__(app)
        self._timeout_seconds = timeout_seconds

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        try:
            return await asyncio.wait_for(
                call_next(request), timeout=self._timeout_seconds
            )
        except TimeoutError:
            return error_response(
                "request_timeout", "Request timed out.", status_code=504
            )


class RequestMetricsMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        metrics: MetricsRegistry,
    ) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        declared_path = getattr(route, "path", "__unmatched__")
        self._metrics.record_http(
            request.method,
            declared_path,
            response.status_code,
            time.perf_counter() - started,
        )
        return response
