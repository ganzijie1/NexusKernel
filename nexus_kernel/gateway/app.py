"""FastAPI application factory for the NexusKernel API role."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from typing import Any, Generic, Literal, TypeVar

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from nexus_kernel.config import ApplicationSettings
from nexus_kernel.errors import ConfigError, FrameworkError, ModuleDependencyError
from nexus_kernel.gateway.middleware import (
    RequestBodyLimitMiddleware,
    RequestContextMiddleware,
    RequestLoggingMiddleware,
    RequestMetricsMiddleware,
    RequestTimeoutMiddleware,
)
from nexus_kernel.gateway.responses import error_response, success_response
from nexus_kernel.kernel import HttpEndpointSpec, Kernel
from nexus_kernel.observability import MetricsRegistry


_ResponseData = TypeVar("_ResponseData")
_PATH_PARAMETER = re.compile(r"{[^}:]+(?P<converter>:[^}]+)?}")


class _ResponseMeta(BaseModel):
    request_id: str | None = None
    trace_id: str | None = None


class _SuccessEnvelope(BaseModel, Generic[_ResponseData]):
    success: Literal[True]
    data: _ResponseData
    meta: _ResponseMeta


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


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
    async def endpoint(request: Request, payload: Any = None) -> Response:
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
                arguments = (payload,)

            result = handler(*arguments)
            if inspect.isawaitable(result):
                if spec.timeout_seconds is None:
                    result = await result
                else:
                    try:
                        result = await asyncio.wait_for(
                            result, timeout=spec.timeout_seconds
                        )
                    except TimeoutError:
                        raise ApiError(
                            "request_timeout", "Request timed out.", 504
                        ) from None
            serialized = _serialize_result(result, spec.response_schema)
            return success_response(serialized, status_code=spec.status_code)
        except ApiError:
            raise
        except ConfigError:
            raise
        except FrameworkError:
            raise
        except Exception:
            raise ApiError(
                "internal_error", "An internal error occurred.", 500
            ) from None

    endpoint.__name__ = f"module_{spec.target_api}"
    parameters = []
    if spec.request_schema is not None:
        parameters.append(
            inspect.Parameter(
                "payload",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=spec.request_schema,
            )
        )
    parameters.append(
        inspect.Parameter(
            "request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request
        )
    )
    setattr(endpoint, "__signature__", inspect.Signature(parameters))
    return endpoint


def _normalize_path(path: str) -> str:
    return _PATH_PARAMETER.sub(
        lambda match: "{" + (match.group("converter") or "") + "}", path
    )


def _router_identities(router: Any) -> Iterator[tuple[str, str]]:
    for route in router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or methods is None:
            continue
        for method in methods:
            yield method.upper(), _normalize_path(path)


def _register_application_routers(
    app: FastAPI,
    routers: Iterable[APIRouter],
    occupied: set[tuple[str, str]],
) -> None:
    for router in routers:
        for identity in _router_identities(router):
            if identity in occupied:
                method, path = identity
                raise ConfigError(f"HTTP route is duplicated: {method} {path}")
            occupied.add(identity)
        app.include_router(router)


def _register_module_routes(
    app: FastAPI,
    kernel: Kernel,
    occupied: set[tuple[str, str]],
) -> None:
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
            identity = (method, _normalize_path(spec.path))
            if identity in occupied:
                raise ConfigError(f"HTTP route is duplicated: {method} {spec.path}")
            occupied.add(identity)
        app.add_api_route(
            spec.path,
            _build_endpoint(handler, spec),
            methods=list(spec.methods),
            status_code=spec.status_code,
            response_model=(
                _SuccessEnvelope[spec.response_schema]  # type: ignore[name-defined]
                if spec.response_schema is not None
                else None
            ),
            tags=list(spec.tags) or [module_name],
        )


def create_app(
    *,
    settings: ApplicationSettings,
    kernel: Kernel,
    metrics: MetricsRegistry | None = None,
    routers: Iterable[APIRouter] = (),
) -> FastAPI:
    metrics = metrics or MetricsRegistry(
        settings.monitoring.metrics_prefix,
        cache_ttl_seconds=settings.monitoring.metrics_cache_ttl_seconds,
    )

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

    occupied = set(_router_identities(app))
    _register_application_routers(app, routers, occupied)
    try:
        _register_module_routes(app, kernel, occupied)
    except ModuleDependencyError:
        raise ConfigError("Invalid module HTTP route declaration") from None

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
    if settings.logging.request_log_enabled:
        app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestContextMiddleware)
    return app
