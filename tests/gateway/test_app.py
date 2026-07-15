from __future__ import annotations

import asyncio
from typing import Any, Protocol

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from app.modules.example import ExampleModule
from nexuskernel.config import ApplicationSettings
from nexuskernel.errors import ConfigError
from nexuskernel.gateway import create_app
from nexuskernel.kernel import HttpEndpointSpec, Kernel, LoadedModule, Module


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
    assert "nexuskernel_http_requests_total" in metrics.text
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


def test_core_application_and_module_yaml_build_default_app() -> None:
    app = create_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/example/echo", json={"message": "configured", "count": 1}
        )

    assert response.status_code == 200
    assert response.json()["data"]["message"] == "configured"
