"""Gateway limits, context propagation, timeouts, and request metrics."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from collections import deque
from typing import Any

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nexus_kernel.gateway.responses import error_response
from nexus_kernel.observability import MetricsRegistry
from nexus_kernel.runtime import bind_request_context, get_request_context


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


class RequestTimeoutMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        timeout_seconds: float,
        max_response_buffer_bytes: int,
        metrics: MetricsRegistry,
    ) -> None:
        self.app = app
        self._timeout_seconds = timeout_seconds
        self._max_response_buffer_bytes = max_response_buffer_bytes
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        buffered = _BufferedHttpResponse(self._max_response_buffer_bytes)

        async def run_application() -> None:
            await self.app(scope, receive, buffered.send)

        app_task: asyncio.Task[None] = asyncio.create_task(run_application())
        ready_task = asyncio.create_task(buffered.ready.wait())
        try:
            async with asyncio.timeout(self._timeout_seconds):
                done, _ = await asyncio.wait(
                    {app_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if ready_task not in done:
                    _raise_response_task_error(app_task)
        except TimeoutError:
            _abandon_response(app_task, buffered)
            await self._send_error(scope, receive, send, "request_timeout", 504)
            return
        except _ResponseTooLarge:
            _abandon_response(app_task, buffered)
            await self._send_error(scope, receive, send, "response_too_large", 500)
            return
        except _InvalidResponse:
            _abandon_response(app_task, buffered)
            await self._send_error(scope, receive, send, "invalid_response", 500)
            return
        except asyncio.CancelledError:
            _abandon_response(app_task, buffered)
            raise
        finally:
            if not ready_task.done():
                ready_task.cancel()
                ready_task.add_done_callback(_consume_task_result)

        start, body = buffered.response_messages()
        try:
            await send(start)
            await send(body)
        except BaseException:
            _abandon_response(app_task, buffered)
            raise

        buffered.commit()
        try:
            await asyncio.shield(app_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                app_task.add_done_callback(self._observe_background_task)
                raise
            self._record_background_failure()
        except BaseException:
            self._record_background_failure()

    async def _send_error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        code: str,
        status_code: int,
    ) -> None:
        messages = {
            "request_timeout": "Request timed out.",
            "response_too_large": "Response exceeds the configured limit.",
            "invalid_response": "Application returned an invalid response.",
        }
        response = error_response(code, messages[code], status_code=status_code)
        await response(scope, receive, send)

    def _observe_background_task(self, task: asyncio.Task[None]) -> None:
        try:
            error = task.exception()
        except asyncio.CancelledError:
            error = asyncio.CancelledError()
        if error is not None:
            self._record_background_failure()

    def _record_background_failure(self) -> None:
        logger.error("http_background_task_failed")
        self._metrics.record_background_failure()


class _ResponseBufferError(Exception):
    pass


class _ResponseTooLarge(_ResponseBufferError):
    pass


class _InvalidResponse(_ResponseBufferError):
    pass


class _BufferedHttpResponse:
    def __init__(self, limit: int) -> None:
        self.ready = asyncio.Event()
        self._release = asyncio.Event()
        self._limit = limit
        self._size = 0
        self._start: Message | None = None
        self._body = bytearray()
        self._complete = False
        self._abandoned = False

    async def send(self, message: Message) -> None:
        if self._abandoned:
            raise _InvalidResponse from None
        message_type = message.get("type")
        if message_type == "http.response.start":
            self._set_start(message)
            return
        if message_type != "http.response.body":
            raise _InvalidResponse from None
        if self._start is None or self._complete:
            raise _InvalidResponse from None
        if set(message) - {"type", "body", "more_body"}:
            raise _InvalidResponse from None
        body = message.get("body", b"")
        more_body = message.get("more_body", False)
        if not isinstance(body, bytes) or not isinstance(more_body, bool):
            raise _InvalidResponse from None
        self._reserve(len(body))
        self._body.extend(body)
        await asyncio.sleep(0)
        if not more_body:
            self._complete = True
            self.ready.set()
            await self._release.wait()
            if self._abandoned:
                raise _InvalidResponse from None

    def _set_start(self, message: Message) -> None:
        if self._start is not None or self._complete:
            raise _InvalidResponse from None
        if set(message) - {"type", "status", "headers"}:
            raise _InvalidResponse from None
        status = message.get("status")
        headers = message.get("headers", [])
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 100 <= status <= 999
            or not isinstance(headers, (list, tuple))
        ):
            raise _InvalidResponse from None
        copied_headers: list[tuple[bytes, bytes]] = []
        for header in headers:
            if not isinstance(header, (list, tuple)) or len(header) != 2:
                raise _InvalidResponse from None
            name, value = header
            if not isinstance(name, bytes) or not isinstance(value, bytes):
                raise _InvalidResponse from None
            self._reserve(len(name) + len(value))
            copied_headers.append((name, value))
        self._start = {
            "type": "http.response.start",
            "status": status,
            "headers": copied_headers,
        }

    def _reserve(self, size: int) -> None:
        if self._size + size > self._limit:
            raise _ResponseTooLarge from None
        self._size += size

    def response_messages(self) -> tuple[Message, Message]:
        if self._start is None or not self._complete:
            raise _InvalidResponse from None
        return self._start, {
            "type": "http.response.body",
            "body": bytes(self._body),
            "more_body": False,
        }

    def commit(self) -> None:
        self._release.set()

    def abandon(self) -> None:
        self._abandoned = True
        self._release.set()


def _raise_response_task_error(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except _ResponseTooLarge:
        raise
    except _InvalidResponse:
        raise
    except BaseException:
        raise _InvalidResponse from None
    raise _InvalidResponse from None


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def _abandon_response(
    task: asyncio.Task[None], buffered: _BufferedHttpResponse
) -> None:
    buffered.abandon()
    if not task.done():
        task.cancel()
    task.add_done_callback(_consume_task_result)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        declared_path = getattr(route, "path", "__unmatched__")
        context = get_request_context()
        logger.bind(
            request_id=context.request_id if context else None,
            trace_id=context.trace_id if context else None,
        ).info(
            "http_request",
            method=request.method,
            route=declared_path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return response


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
