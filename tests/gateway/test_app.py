from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Protocol

import fastapi
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from starlette.background import BackgroundTask
from starlette.convertors import CONVERTOR_TYPES, Convertor
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Scope

from app.modules.example import ExampleModule
from nexus_kernel.config import ApplicationSettings
from nexus_kernel.errors import ConfigError
from nexus_kernel.gateway import create_app
from nexus_kernel.gateway.app import ApiError, _build_endpoint
from nexus_kernel.gateway.middleware import RequestTimeoutMiddleware
from nexus_kernel.kernel import HttpEndpointSpec, Kernel, LoadedModule, Module
from nexus_kernel.observability import MetricsRegistry


def _loaded(module: Module, index: int = 0) -> LoadedModule:
    return LoadedModule(module=module, required=True, original_index=index)


def _settings(**server_overrides: Any) -> ApplicationSettings:
    return ApplicationSettings.model_validate(
        {
            "server": server_overrides,
            "monitoring": {"metrics_cache_ttl_seconds": 1},
        }
    )


def _client(
    module: Module | None = None, *, settings: ApplicationSettings | None = None
) -> TestClient:
    modules = [] if module is None else [_loaded(module)]
    app = create_app(settings=settings or _settings(), kernel=Kernel(modules))
    return TestClient(app, raise_server_exceptions=False)


def test_live_ready_metrics_and_declarative_echo_route() -> None:
    with _client(ExampleModule()) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        echo = client.post(
            "/api/v1/example/echo", json={"message": "hello", "count": 2}
        )
        metrics = client.get("/metrics")

    assert live.status_code == 200
    assert live.json()["data"] == {"status": "alive"}
    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "healthy"
    assert echo.status_code == 200
    assert echo.json()["data"] == {"message": "hello", "count": 2}
    assert metrics.status_code == 200
    assert "nexus_kernel_http_requests_total" in metrics.text
    assert 'route="/api/v1/example/echo"' in metrics.text


def test_ready_uses_the_complete_runtime_health_provider() -> None:
    kernel = Kernel([])
    app = create_app(
        settings=_settings(),
        kernel=kernel,
        health_provider=lambda: {
            **kernel.health_status(),
            "status": "degraded",
            "agent_runs": {"status": "degraded"},
        },
    )

    with TestClient(app) as client:
        ready = client.get("/health/ready")
        metrics = client.get("/metrics")

    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "degraded"
    assert ready.json()["data"]["agent_runs"] == {"status": "degraded"}
    assert 'component="agent_runs",kind="service"} 0.0' in metrics.text


def test_request_and_trace_ids_are_validated_and_returned() -> None:
    with _client() as client:
        response = client.get(
            "/health/live",
            headers={
                "x-request-id": "request-123",
                "x-trace-id": "trace-456",
            },
        )
        rejected = client.get("/health/live", headers={"x-request-id": "bad value"})

    assert response.headers["x-request-id"] == "request-123"
    assert response.headers["x-trace-id"] == "trace-456"
    assert response.json()["meta"] == {
        "request_id": "request-123",
        "trace_id": "trace-456",
    }
    assert rejected.status_code == 400
    assert rejected.headers["x-request-id"]
    assert rejected.headers["x-trace-id"]


def test_request_logging_uses_safe_structured_fields(monkeypatch) -> None:
    records: list[tuple[str, dict[str, Any]]] = []

    class CapturingLogger:
        def bind(self, **values: Any) -> CapturingLogger:
            records.append(("bind", values))
            return self

        def info(self, message: str, **values: Any) -> None:
            records.append((message, values))

    monkeypatch.setattr("nexus_kernel.gateway.middleware.logger", CapturingLogger())
    with _client() as client:
        client.get(
            "/health/live",
            headers={
                "authorization": "Bearer do-not-log",  # security-test-fixture
                "x-request-id": "safe-id",
            },
        )

    serialized = repr(records)
    assert "do-not-log" not in serialized
    assert any(
        message == "http_request"
        and values["method"] == "GET"
        and values["route"] == "/health/live"
        and values["status"] == 200
        for message, values in records
    )


def test_request_body_limit_returns_safe_413_envelope() -> None:
    with _client(
        ExampleModule(), settings=_settings(max_request_body_bytes=16)
    ) as client:
        response = client.post(
            "/api/v1/example/echo",
            content=b'{"message":"this is too large"}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"
    assert "this is too large" not in response.text


def test_streamed_request_body_is_limited_without_content_length() -> None:
    def chunks() -> Any:
        yield b'{"message":"'
        yield b"streamed content exceeds limit"
        yield b'"}'

    with _client(
        ExampleModule(), settings=_settings(max_request_body_bytes=16)
    ) as client:
        response = client.post(
            "/api/v1/example/echo",
            content=chunks(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlowApi(Protocol):
    async def wait(self, request: EmptyRequest) -> dict[str, bool]: ...


class SlowModule(Module):
    name = "slow"
    api_contract = SlowApi
    http_endpoints = (
        HttpEndpointSpec(
            path="/api/v1/slow",
            methods=("POST",),
            target_api="wait",
            request_schema=EmptyRequest,
            timeout_seconds=0.01,
        ),
    )

    async def wait(self, request: EmptyRequest) -> dict[str, bool]:
        await asyncio.sleep(60)
        return {"done": True}


def test_declarative_route_timeout_returns_504() -> None:
    with _client(SlowModule()) as client:
        response = client.post("/api/v1/slow", json={})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "request_timeout"


@pytest.mark.asyncio
async def test_sync_declarative_handler_runs_in_thread_and_awaits_returned_value() -> (
    None
):
    event_loop_thread_id = threading.get_ident()
    handler_thread_ids: list[int] = []

    def handler(request: EmptyRequest) -> Any:
        handler_thread_ids.append(threading.get_ident())

        async def finish() -> dict[str, bool]:
            await asyncio.sleep(0)
            return {"done": True}

        return finish()

    endpoint = _build_endpoint(
        handler,
        HttpEndpointSpec(
            path="/api/v1/sync-awaitable",
            methods=("POST",),
            target_api="wait",
            request_schema=EmptyRequest,
            timeout_seconds=1.0,
        ),
    )
    response = await endpoint(
        Request({"type": "http", "method": "POST", "path": "/"}),
        EmptyRequest(),
    )

    assert response.status_code == 200
    assert json.loads(response.body)["data"] == {"done": True}
    assert handler_thread_ids
    assert handler_thread_ids[0] != event_loop_thread_id


@pytest.mark.asyncio
async def test_sync_declarative_handler_timeout_discards_late_result() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def handler(request: EmptyRequest) -> dict[str, bool]:
        started.set()
        release.wait(timeout=1)
        finished.set()
        return {"late": True}

    endpoint = _build_endpoint(
        handler,
        HttpEndpointSpec(
            path="/api/v1/blocking-sync",
            methods=("POST",),
            target_api="wait",
            request_schema=EmptyRequest,
            timeout_seconds=0.01,
        ),
    )
    with pytest.raises(ApiError) as captured:
        await endpoint(
            Request({"type": "http", "method": "POST", "path": "/"}),
            EmptyRequest(),
        )

    assert started.is_set()
    assert captured.value.status_code == 504
    assert captured.value.code == "request_timeout"
    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert "late" not in captured.value.message


@pytest.mark.asyncio
async def test_global_request_timeout_bounds_sync_declarative_handler() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def handler(request: EmptyRequest) -> dict[str, bool]:
        started.set()
        release.wait(timeout=1)
        finished.set()
        return {"late": True}

    endpoint = _build_endpoint(
        handler,
        HttpEndpointSpec(
            path="/api/v1/globally-bounded-sync",
            methods=("POST",),
            target_api="wait",
            request_schema=EmptyRequest,
        ),
    )

    async def application(scope: Scope, receive: Any, send: Any) -> None:
        response = await endpoint(Request(scope, receive), EmptyRequest())
        await response(scope, receive, send)

    middleware = RequestTimeoutMiddleware(
        application,
        timeout_seconds=0.01,
        max_response_buffer_bytes=1024,
        metrics=MetricsRegistry("sync_global_timeout", cache_ttl_seconds=1),
    )
    request_pending = True

    async def receive() -> Message:
        nonlocal request_pending
        if request_pending:
            request_pending = False
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await middleware(_http_scope(), receive, send)

    assert started.is_set()
    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0)
    assert [message["type"] for message in messages] == [
        "http.response.start",
        "http.response.body",
    ]
    assert messages[0]["status"] == 504
    payload = json.loads(messages[1]["body"])
    assert payload["error"]["code"] == "request_timeout"
    assert "late" not in payload


class OptionalRouteApi(Protocol):
    async def read(self) -> dict[str, bool]: ...


class FailingOptionalRouteModule(Module):
    name = "failing_optional_route"
    api_contract = OptionalRouteApi
    http_endpoints = (
        HttpEndpointSpec(
            path="/api/v1/failing-optional",
            methods=("GET",),
            target_api="read",
        ),
    )

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def on_start(self, context: Any) -> None:
        raise RuntimeError("private startup failure")

    async def read(self) -> dict[str, bool]:
        self.calls += 1
        return {"called": True}


def test_degraded_optional_module_route_returns_503_without_calling_handler() -> None:
    module = FailingOptionalRouteModule()
    kernel = Kernel([LoadedModule(module, required=False, original_index=0)])
    app = create_app(settings=_settings(), kernel=kernel)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/failing-optional")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert module.calls == 0


def test_validation_and_unknown_routes_use_safe_error_envelopes() -> None:
    with _client(ExampleModule()) as client:
        invalid = client.post(
            "/api/v1/example/echo",
            json={"message": "do-not-reflect", "unexpected": "secret"},
        )
        missing = client.get("/does-not-exist")

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert invalid.headers["Cache-Control"] == "no-store"
    assert "do-not-reflect" not in invalid.text
    assert "secret" not in invalid.text
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert missing.headers["Cache-Control"] == "no-store"


class DuplicateModule(Module):
    name = "duplicate"
    api_contract = SlowApi
    http_endpoints = (
        HttpEndpointSpec(
            path="/health/live",
            methods=("GET",),
            target_api="wait",
            request_schema=EmptyRequest,
        ),
    )

    async def wait(self, request: EmptyRequest) -> dict[str, bool]:
        return {"done": True}


def test_gateway_rejects_duplicate_declared_routes() -> None:
    with pytest.raises(ConfigError, match="duplicate"):
        create_app(settings=_settings(), kernel=Kernel([_loaded(DuplicateModule())]))


def test_gateway_requires_explicit_settings_and_kernel() -> None:
    with pytest.raises(TypeError):
        create_app()  # type: ignore[call-arg]


def test_gateway_registers_application_routers() -> None:
    router = APIRouter()

    @router.get("/api/v1/application")
    async def application_route() -> dict[str, bool]:
        return {"application": True}

    app = create_app(settings=_settings(), kernel=Kernel([]), routers=(router,))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/application")

    assert response.status_code == 200
    assert response.json() == {"application": True}


def test_gateway_rejects_router_conflicts_with_builtin_and_other_routers() -> None:
    builtin_conflict = APIRouter()

    @builtin_conflict.get("/health/live")
    async def duplicate_live() -> dict[str, bool]:
        return {"duplicate": True}

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(settings=_settings(), kernel=Kernel([]), routers=(builtin_conflict,))

    first = APIRouter()
    second = APIRouter()

    @first.post("/api/v1/shared")
    async def first_shared() -> dict[str, int]:
        return {"owner": 1}

    @second.post("/api/v1/shared")
    async def second_shared() -> dict[str, int]:
        return {"owner": 2}

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(settings=_settings(), kernel=Kernel([]), routers=(first, second))


def test_gateway_rejects_router_conflicts_with_fastapi_system_routes() -> None:
    router = APIRouter()

    @router.get("/openapi.json")
    async def duplicate_openapi() -> dict[str, bool]:
        return {"duplicate": True}

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(settings=_settings(), kernel=Kernel([]), routers=(router,))


def test_declarative_route_schemas_are_published_in_openapi() -> None:
    with _client(ExampleModule()) as client:
        document = client.get("/openapi.json").json()
        operation = document["paths"]["/api/v1/example/echo"]["post"]

    assert "EchoRequest" in str(operation["requestBody"])
    assert "EchoResponse" in str(operation["responses"]["200"])
    response_ref = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    response_schema = document["components"]["schemas"][response_ref.rsplit("/", 1)[1]]
    assert set(response_schema["properties"]) == {"success", "data", "meta"}
    assert set(response_schema["required"]) == {"success", "data", "meta"}


def test_gateway_rejects_semantically_duplicate_dynamic_paths() -> None:
    first = APIRouter()
    second = APIRouter()

    @first.get("/api/v1/users/{user_id}")
    async def first_user(user_id: str) -> dict[str, str]:
        return {"user_id": user_id}

    @second.get("/api/v1/users/{name}")
    async def second_user(name: str) -> dict[str, str]:
        return {"name": name}

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(settings=_settings(), kernel=Kernel([]), routers=(first, second))


def test_response_buffer_limit_returns_safe_500() -> None:
    router = APIRouter()
    metrics = MetricsRegistry("response_limit_test", cache_ttl_seconds=0.01)

    @router.get("/api/v1/large-response")
    async def large_response() -> Response:
        return Response(content=b"x" * 2048, media_type="application/octet-stream")

    app = create_app(
        settings=_settings(max_response_buffer_bytes=512),
        kernel=Kernel([]),
        routers=(router,),
        metrics=metrics,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/large-response", headers={"origin": "https://example.test"}
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "response_too_large"
    assert "x" * 32 not in response.text
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["x-request-id"]
    assert response.headers["x-trace-id"]
    rendered = metrics.render()
    assert b'route="/api/v1/large-response",status="500"' in rendered


def test_server_deadline_preserves_cors_ids_and_metrics() -> None:
    router = APIRouter()
    metrics = MetricsRegistry("server_timeout_test", cache_ttl_seconds=0.01)

    @router.get("/api/v1/server-timeout")
    async def server_timeout() -> dict[str, bool]:
        await asyncio.sleep(60)
        return {"done": True}

    app = create_app(
        settings=_settings(request_timeout_seconds=0.01),
        kernel=Kernel([]),
        routers=(router,),
        metrics=metrics,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/server-timeout", headers={"origin": "https://example.test"}
        )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "request_timeout"
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["x-request-id"]
    assert response.headers["x-trace-id"]
    assert b'route="/api/v1/server-timeout",status="504"' in metrics.render()


def _http_scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }


async def _run_timeout_middleware(
    app: ASGIApp, *, timeout: float = 1.0, limit: int = 4096
) -> tuple[list[Message], MetricsRegistry]:
    messages: list[Message] = []
    metrics = MetricsRegistry("gateway_test", cache_ttl_seconds=0.01)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(dict(message))

    middleware = RequestTimeoutMiddleware(
        app,
        timeout_seconds=timeout,
        max_response_buffer_bytes=limit,
        metrics=metrics,
    )
    await middleware(_http_scope(), receive, send)
    return messages, metrics


def _message_payload(messages: list[Message]) -> dict[str, Any]:
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return json.loads(body)


@pytest.mark.asyncio
async def test_background_task_runs_after_response_commit() -> None:
    events: list[str] = []

    async def background() -> None:
        events.append("background")
        raise RuntimeError("background failure")

    response = Response(content=b"ok", background=BackgroundTask(background))

    async def application(scope: Scope, receive: Any, send: Any) -> None:
        await response(scope, receive, send)

    messages: list[Message] = []
    metrics = MetricsRegistry("background_test", cache_ttl_seconds=0.01)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        events.append(message["type"])
        messages.append(dict(message))

    middleware = RequestTimeoutMiddleware(
        application,
        timeout_seconds=1,
        max_response_buffer_bytes=4096,
        metrics=metrics,
    )
    await middleware(_http_scope(), receive, send)

    assert events == ["http.response.start", "http.response.body", "background"]
    assert messages[0]["status"] == 200
    assert messages[1]["body"] == b"ok"
    assert b"gateway" not in metrics.render()
    assert b"background_test_http_background_task_failures_total 1.0" in (
        metrics.render()
    )


@pytest.mark.asyncio
async def test_slow_background_task_is_outside_server_deadline() -> None:
    events: list[str] = []

    async def background() -> None:
        events.append("background:start")
        await asyncio.sleep(0.15)
        events.append("background:done")

    response = Response(content=b"ok", background=BackgroundTask(background))

    async def application(scope: Scope, receive: Any, send: Any) -> None:
        await response(scope, receive, send)

    messages: list[Message] = []
    metrics = MetricsRegistry("slow_background_test", cache_ttl_seconds=0.01)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        events.append(message["type"])
        messages.append(dict(message))

    middleware = RequestTimeoutMiddleware(
        application,
        timeout_seconds=0.1,
        max_response_buffer_bytes=4096,
        metrics=metrics,
    )
    await middleware(_http_scope(), receive, send)

    assert events == [
        "http.response.start",
        "http.response.body",
        "background:start",
        "background:done",
    ]
    assert messages[0]["status"] == 200
    assert messages[1]["body"] == b"ok"
    assert b"slow_background_test_http_background_task_failures_total 0.0" in (
        metrics.render()
    )


@pytest.mark.asyncio
async def test_empty_response_chunks_cannot_starve_server_deadline() -> None:
    async def application(scope: Scope, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            await send({"type": "http.response.body", "body": b"", "more_body": True})

    messages, _ = await _run_timeout_middleware(application, timeout=0.01)

    assert messages[0]["status"] == 504
    assert _message_payload(messages)["error"]["code"] == "request_timeout"


@pytest.mark.asyncio
async def test_response_header_limit_stops_before_copying_later_headers() -> None:
    visited: list[tuple[bytes, bytes]] = []

    class TrackingHeaders(list[tuple[bytes, bytes]]):
        def __iter__(self):
            for header in super().__iter__():
                visited.append(header)
                yield header

    headers = TrackingHeaders(
        [
            (b"x-large", b"0123456789"),
            (b"x-later", b"must-not-be-copied"),
        ]
    )

    async def application(scope: Scope, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": headers})

    messages, _ = await _run_timeout_middleware(application, limit=8)

    assert messages[0]["status"] == 500
    assert _message_payload(messages)["error"]["code"] == "response_too_large"
    assert visited == [headers[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["duplicate_start", "extension", "no_body"])
async def test_invalid_asgi_responses_fail_closed(invalid_kind: str) -> None:
    async def application(scope: Scope, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if invalid_kind == "duplicate_start":
            await send({"type": "http.response.start", "status": 200, "headers": []})
        elif invalid_kind == "extension":
            await send({"type": "http.response.trailers", "headers": []})

    messages, _ = await _run_timeout_middleware(application)

    assert messages[0]["status"] == 500
    assert _message_payload(messages)["error"]["code"] == "invalid_response"


def test_gateway_rejects_nested_router_conflicts() -> None:
    leaf = APIRouter()

    @leaf.get("/items/{item_id:int}")
    async def nested_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    middle = APIRouter()
    middle.include_router(leaf, prefix="/users")
    outer = APIRouter()
    outer.include_router(middle, prefix="/api/v1")
    conflict = APIRouter()

    @conflict.get("/api/v1/users/items/{name}")
    async def conflicting_item(name: str) -> dict[str, str]:
        return {"name": name}

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(settings=_settings(), kernel=Kernel([]), routers=(outer, conflict))


def test_gateway_rejects_mount_conflicts() -> None:
    mounted = FastAPI()

    @mounted.get("/{path:path}")
    async def mounted_path(path: str) -> dict[str, str]:
        return {"path": path}

    owner = APIRouter()
    owner.mount("/assets", mounted)
    conflict = APIRouter()

    @conflict.get("/assets/readme")
    async def asset_readme() -> dict[str, bool]:
        return {"ok": True}

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(settings=_settings(), kernel=Kernel([]), routers=(owner, conflict))


def test_gateway_treats_unknown_converters_as_overlapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UpperConvertor(Convertor[str]):
        regex = "[A-Z]+"

        def convert(self, value: str) -> str:
            return value

        def to_string(self, value: str) -> str:
            return value

    monkeypatch.setitem(CONVERTOR_TYPES, "upper", UpperConvertor())
    parameterized = APIRouter()
    literal = APIRouter()

    @parameterized.get("/api/v1/codes/{value:upper}")
    async def code(value: str) -> dict[str, str]:
        return {"value": value}

    @literal.get("/api/v1/codes/123")
    async def numeric_code() -> dict[str, str]:
        return {"value": "123"}

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(
            settings=_settings(),
            kernel=Kernel([]),
            routers=(parameterized, literal),
        )


@pytest.mark.parametrize(
    ("first_converter", "second_converter"),
    [
        ("int", "float"),
        ("float", "int"),
        ("int", "uuid"),
        ("uuid", "int"),
        ("float", "uuid"),
        ("uuid", "float"),
    ],
)
def test_gateway_rejects_overlapping_numeric_and_uuid_converter_domains(
    first_converter: str, second_converter: str
) -> None:
    first = APIRouter()
    second = APIRouter()

    async def first_value(value: Any) -> dict[str, Any]:
        return {"value": value}

    async def second_value(value: Any) -> dict[str, Any]:
        return {"value": value}

    first.add_api_route(
        f"/api/v1/values/{{value:{first_converter}}}", first_value, methods=["GET"]
    )
    second.add_api_route(
        f"/api/v1/values/{{value:{second_converter}}}",
        second_value,
        methods=["GET"],
    )

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(
            settings=_settings(),
            kernel=Kernel([]),
            routers=(first, second),
        )


def test_gateway_rejects_trailing_slash_and_cross_owner_conflicts() -> None:
    application = APIRouter()

    @application.post("/api/v1/example/echo/")
    async def application_echo() -> dict[str, bool]:
        return {"ok": True}

    with pytest.raises(ConfigError, match="duplicated"):
        create_app(
            settings=_settings(),
            kernel=Kernel([_loaded(ExampleModule())]),
            routers=(application,),
        )


def test_fastapi_private_router_contract_is_pinned() -> None:
    assert fastapi.__version__ == "0.139.0"

    child = APIRouter()

    @child.get("/child")
    async def child_route() -> dict[str, bool]:
        return {"ok": True}

    parent = APIRouter()
    parent.include_router(child, prefix="/nested")
    included = next(
        route for route in parent.routes if hasattr(route, "original_router")
    )
    assert included.original_router is child
    assert included.include_context.prefix == "/nested"


def test_gateway_rejects_included_router_cycles() -> None:
    first = APIRouter()
    second = APIRouter()
    first.include_router(second, prefix="/second")

    holder = APIRouter()
    holder.include_router(first, prefix="/first")
    second.routes.append(holder.routes[0])

    with pytest.raises(ConfigError, match="inclusion cycle"):
        create_app(settings=_settings(), kernel=Kernel([]), routers=(first,))


def test_lifespan_propagates_kernel_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = Kernel([])

    async def failing_stop() -> list[str]:
        return ["module_stop_timeout:test"]

    monkeypatch.setattr(kernel, "stop", failing_stop)
    app = create_app(settings=_settings(), kernel=kernel)

    with pytest.raises(RuntimeError, match="kernel_shutdown_failed"):
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/health/live").status_code == 200


@pytest.mark.asyncio
async def test_lifespan_redacts_raised_kernel_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = Kernel([])
    logs: list[str] = []

    async def failing_stop() -> list[str]:
        raise RuntimeError("private shutdown detail")

    monkeypatch.setattr(kernel, "stop", failing_stop)
    monkeypatch.setattr(
        "nexus_kernel.gateway.app.logger.error",
        lambda message: logs.append(message),
    )
    app = create_app(settings=_settings(), kernel=kernel)

    with pytest.raises(RuntimeError, match="^kernel_shutdown_failed$") as captured:
        async with app.router.lifespan_context(app):
            pass

    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True
    assert "private shutdown detail" not in str(captured.value)
    assert logs == ["kernel_shutdown_failed"]


@pytest.mark.asyncio
async def test_lifespan_propagates_kernel_shutdown_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = Kernel([])
    logs: list[str] = []

    async def cancelled_stop() -> list[str]:
        raise asyncio.CancelledError

    monkeypatch.setattr(kernel, "stop", cancelled_stop)
    monkeypatch.setattr(
        "nexus_kernel.gateway.app.logger.error",
        lambda message: logs.append(message),
    )
    app = create_app(settings=_settings(), kernel=kernel)

    with pytest.raises(asyncio.CancelledError):
        async with app.router.lifespan_context(app):
            pass

    assert logs == ["kernel_shutdown_failed"]
