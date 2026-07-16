from __future__ import annotations

import asyncio
from typing import Any, Protocol

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from app.modules.example import ExampleModule
from nexus_kernel.config import ApplicationSettings
from nexus_kernel.errors import ConfigError
from nexus_kernel.gateway import create_app
from nexus_kernel.kernel import HttpEndpointSpec, Kernel, LoadedModule, Module


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
    assert "do-not-reflect" not in invalid.text
    assert "secret" not in invalid.text
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


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
