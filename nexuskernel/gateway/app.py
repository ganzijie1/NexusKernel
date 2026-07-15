"""FastAPI application factory for the NexusKernel API role."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from nexuskernel.ai import (
    AiProviderUnavailableError,
    AuthNotConfiguredError,
    ChatRequest,
    ChatService,
    DisabledChatService,
    UnauthorizedError,
)
from nexuskernel.config import (
    ApplicationSettings,
    load_application_settings,
    load_module_definitions,
)
from nexuskernel.errors import ConfigError, FrameworkError, ModuleDependencyError
from nexuskernel.gateway.middleware import (
    RequestBodyLimitMiddleware,
    RequestContextMiddleware,
    RequestMetricsMiddleware,
    RequestTimeoutMiddleware,
)
from nexuskernel.gateway.responses import error_response, success_response
from nexuskernel.kernel import HttpEndpointSpec, Kernel, load_modules
from nexuskernel.observability import MetricsRegistry


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BUILTIN_ROUTES = {
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    ("GET", "/metrics"),
    ("POST", "/api/v1/chat"),
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


def _default_settings() -> ApplicationSettings:
    return load_application_settings(_PROJECT_ROOT / "config" / "application.yaml")


def _default_kernel() -> Kernel:
    definitions = load_module_definitions(_PROJECT_ROOT / "config" / "modules.yaml")
    return Kernel(load_modules(definitions))


def _serialize_result(result: Any, response_schema: Any) -> Any:
    if response_schema is None:
        return result
    if not isinstance(response_schema, type) or not issubclass(
        response_schema, BaseModel
    ):
        raise ConfigError("HTTP response_schema must be a Pydantic model type")
    return response_schema.model_validate(result)


def _build_endpoint(
    handler: Callable[..., Any], spec: HttpEndpointSpec
) -> Callable[[Request], Any]:
    async def endpoint(request: Request) -> Response:
        try:
            if spec.request_schema is None:
                arguments: tuple[Any, ...] = ()
            else:
                if not isinstance(spec.request_schema, type) or not issubclass(
                    spec.request_schema, BaseModel
                ):
                    raise ConfigError(
                        "HTTP request_schema must be a Pydantic model type"
                    )
                try:
                    payload = await request.json()
                    parsed = spec.request_schema.model_validate(payload)
                except (ValueError, ValidationError) as exc:
                    raise ApiError(
                        "invalid_request", "Request validation failed.", 422
                    ) from exc
                arguments = (parsed,)

            result = handler(*arguments)
            if inspect.isawaitable(result):
                if spec.timeout_seconds is None:
                    result = await result
                else:
                    try:
                        result = await asyncio.wait_for(
                            result, timeout=spec.timeout_seconds
                        )
                    except TimeoutError as exc:
                        raise ApiError(
                            "request_timeout", "Request timed out.", 504
                        ) from exc
            serialized = _serialize_result(result, spec.response_schema)
            return success_response(serialized, status_code=spec.status_code)
        except ApiError:
            raise
        except ConfigError:
            raise
        except Exception as exc:
            raise ApiError(
                "internal_error", "An internal error occurred.", 500
            ) from exc

    endpoint.__name__ = f"module_{spec.target_api}"
    return endpoint


def _register_module_routes(app: FastAPI, kernel: Kernel) -> None:
    occupied = set(_BUILTIN_ROUTES)
    for binding in kernel.route_bindings():
        module_name = binding.module_name
        spec = binding.spec
        handler = binding.handler
        if spec.auth_required:
            raise ConfigError(
                "Generic module-route authentication is not supported in V1"
            )
        for schema_name, schema in (
            ("request_schema", spec.request_schema),
            ("response_schema", spec.response_schema),
        ):
            if schema is not None and (
                not isinstance(schema, type) or not issubclass(schema, BaseModel)
            ):
                raise ConfigError(f"HTTP {schema_name} must be a Pydantic model type")
        for method in spec.methods:
            identity = (method, spec.path)
            if identity in occupied:
                raise ConfigError(f"HTTP route is duplicated: {method} {spec.path}")
            occupied.add(identity)
        app.add_api_route(
            spec.path,
            _build_endpoint(handler, spec),
            methods=list(spec.methods),
            status_code=spec.status_code,
            tags=list(spec.tags) or [module_name],
        )


def create_app(
    *,
    settings: ApplicationSettings | None = None,
    kernel: Kernel | None = None,
    metrics: MetricsRegistry | None = None,
    chat_service: ChatService | None = None,
) -> FastAPI:
    settings = settings or _default_settings()
    kernel = kernel or _default_kernel()
    metrics = metrics or MetricsRegistry(
        settings.monitoring.metrics_prefix,
        cache_ttl_seconds=settings.monitoring.metrics_cache_ttl_seconds,
    )
    chat_service = chat_service or DisabledChatService()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await kernel.start()
        try:
            yield
        finally:
            app.state.shutdown_errors = await kernel.stop()

    app = FastAPI(
        title=settings.service.name,
        version=settings.service.version,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.kernel = kernel
    app.state.metrics = metrics

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> Response:
        return error_response(exc.code, exc.message, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        return error_response(
            "invalid_request", "Request validation failed.", status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        code = "not_found" if exc.status_code == 404 else "http_error"
        message = "Resource not found." if exc.status_code == 404 else "HTTP error."
        return error_response(code, message, status_code=exc.status_code)

    @app.exception_handler(FrameworkError)
    async def handle_framework_error(request: Request, exc: FrameworkError) -> Response:
        return error_response(
            "service_unavailable", "Service is unavailable.", status_code=503
        )

    @app.exception_handler(AuthNotConfiguredError)
    async def handle_auth_not_configured(
        request: Request, exc: AuthNotConfiguredError
    ) -> Response:
        return error_response(
            "auth_not_configured",
            "Chat authentication is not configured.",
            status_code=503,
        )

    @app.exception_handler(UnauthorizedError)
    async def handle_unauthorized(request: Request, exc: UnauthorizedError) -> Response:
        return error_response(
            "unauthorized", "Authentication is required.", status_code=401
        )

    @app.exception_handler(AiProviderUnavailableError)
    async def handle_ai_unavailable(
        request: Request, exc: AiProviderUnavailableError
    ) -> Response:
        return error_response(
            "ai_provider_unavailable",
            "AI provider is unavailable.",
            status_code=503,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> Response:
        return error_response(
            "internal_error", "An internal error occurred.", status_code=500
        )

    @app.get("/health/live", include_in_schema=False)
    async def live() -> Response:
        return success_response({"status": "alive"})

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> Response:
        try:
            await asyncio.wait_for(
                kernel.providers.refresh_health(),
                timeout=settings.monitoring.health_timeout_seconds,
            )
            health = kernel.health_status()
        except TimeoutError:
            health = {"status": "degraded", "modules": [], "providers": []}
        for item in health["modules"]:
            metrics.set_component_health(
                "module", item["name"], healthy=item["state"] == "ready"
            )
        for item in health["providers"]:
            metrics.set_component_health(
                "provider", item["name"], healthy=item["state"] == "ready"
            )
        return success_response(health)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(content=metrics.render(), media_type=metrics.content_type)

    @app.post("/api/v1/chat", tags=["chat"])
    async def chat(request: ChatRequest, http_request: Request) -> Response:
        response = await chat_service.chat(
            request, http_request.headers.get("authorization")
        )
        return success_response(response)

    try:
        _register_module_routes(app, kernel)
    except ModuleDependencyError as exc:
        raise ConfigError("Invalid module HTTP route declaration") from exc

    app.add_middleware(
        RequestMetricsMiddleware,
        metrics=metrics,
    )
    app.add_middleware(
        RequestTimeoutMiddleware,
        timeout_seconds=settings.server.request_timeout_seconds,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        limit=settings.server.max_request_body_bytes,
    )
    if settings.cors.enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors.allow_origins,
            allow_methods=settings.cors.allow_methods,
            allow_headers=settings.cors.allow_headers,
        )
    app.add_middleware(RequestContextMiddleware)
    return app
