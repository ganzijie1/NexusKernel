"""FastAPI application factory for the NexusKernel API role."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from loguru import logger
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.convertors import CONVERTOR_TYPES
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.routing import Mount

from nexus_kernel.config import ApplicationSettings
from nexus_kernel.errors import ConfigError, FrameworkError, ModuleDependencyError
from nexus_kernel.gateway.middleware import (
    RequestBodyLimitMiddleware,
    RequestContextMiddleware,
    RequestLoggingMiddleware,
    RequestMetricsMiddleware,
    RequestTimeoutMiddleware,
)
from nexus_kernel.gateway.responses import (
    SuccessEnvelope,
    error_response,
    success_response,
)
from nexus_kernel.kernel import HttpEndpointSpec, Kernel
from nexus_kernel.observability import MetricsRegistry


_PATH_PARAMETER = re.compile(r"{[^}:]+(?P<converter>:[^}]+)?}")
_WHOLE_PATH_PARAMETER = re.compile(
    r"^{[A-Za-z_][A-Za-z0-9_]*(?::(?P<converter>[A-Za-z_][A-Za-z0-9_]*))?}$"
)
_KNOWN_CONVERTERS = frozenset({"float", "int", "path", "str", "uuid"})


@dataclass(frozen=True, slots=True)
class _PathSegment:
    literal: str | None = None
    converter: str | None = None


@dataclass(frozen=True, slots=True)
class _RouteIdentity:
    method: str | None
    path: str


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


def _consume_late_handler_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    task.exception()


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

            handler_is_async = inspect.iscoroutinefunction(handler)

            async def invoke() -> Any:
                if handler_is_async:
                    result = handler(*arguments)
                else:
                    result = await asyncio.to_thread(handler, *arguments)
                if inspect.isawaitable(result):
                    return await result
                return result

            late_task: asyncio.Task[Any] | None = None
            invocation: Any
            if handler_is_async:
                invocation = invoke()
            else:
                late_task = asyncio.create_task(invoke())
                invocation = asyncio.shield(late_task)
            try:
                if spec.timeout_seconds is None:
                    result = await invocation
                else:
                    result = await asyncio.wait_for(
                        invocation, timeout=spec.timeout_seconds
                    )
            except TimeoutError:
                if late_task is not None:
                    late_task.add_done_callback(_consume_late_handler_result)
                raise ApiError("request_timeout", "Request timed out.", 504) from None
            except asyncio.CancelledError:
                if late_task is not None:
                    late_task.add_done_callback(_consume_late_handler_result)
                raise
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
    if path == "/":
        return path
    return path.rstrip("/") or "/"


def _path_segments(path: str) -> tuple[_PathSegment, ...]:
    normalized = _normalize_path(path)
    if normalized == "/":
        return ()
    segments: list[_PathSegment] = []
    for value in normalized.lstrip("/").split("/"):
        parameter = _WHOLE_PATH_PARAMETER.fullmatch(value)
        if parameter is not None:
            segments.append(
                _PathSegment(converter=parameter.group("converter") or "str")
            )
        elif _PATH_PARAMETER.search(value) is not None:
            # Mixed literal/parameter segments are uncommon and hard to intersect
            # exactly. Treating them as unknown is deliberately fail-closed.
            segments.append(_PathSegment(converter="unknown"))
        else:
            segments.append(_PathSegment(literal=value))
    return tuple(segments)


def _literal_matches(converter: str, literal: str) -> bool:
    if converter not in _KNOWN_CONVERTERS:
        return True
    definition = CONVERTOR_TYPES.get(converter)
    if definition is None:
        return True
    return re.fullmatch(definition.regex, literal) is not None


def _parameter_domains_overlap(first: str, second: str) -> bool:
    if first not in _KNOWN_CONVERTERS or second not in _KNOWN_CONVERTERS:
        return True
    if first == "path" or second == "path":
        return True
    if first in {"unknown", "str"} or second in {"unknown", "str"}:
        return True
    if first == second:
        return True
    return {first, second}.issubset({"float", "int", "uuid"})


def _segments_overlap(first: _PathSegment, second: _PathSegment) -> bool:
    if first.literal is not None and second.literal is not None:
        return first.literal == second.literal
    if first.literal is not None:
        return _literal_matches(second.converter or "unknown", first.literal)
    if second.literal is not None:
        return _literal_matches(first.converter or "unknown", second.literal)
    return _parameter_domains_overlap(
        first.converter or "unknown", second.converter or "unknown"
    )


def _paths_overlap(first: str, second: str) -> bool:
    first_segments = _path_segments(first)
    second_segments = _path_segments(second)
    for index, (first_segment, second_segment) in enumerate(
        zip(first_segments, second_segments)
    ):
        if not _segments_overlap(first_segment, second_segment):
            return False
        if first_segment.converter == "path" or second_segment.converter == "path":
            return True
        if index == len(first_segments) - 1 or index == len(second_segments) - 1:
            break
    if len(first_segments) == len(second_segments):
        return True
    shorter, longer = sorted((first_segments, second_segments), key=len)
    remaining = longer[len(shorter) :]
    return len(remaining) == 1 and remaining[0].converter == "path"


def _join_route_path(prefix: str, path: str) -> str:
    if not prefix:
        return _normalize_path(path)
    if not path:
        return _normalize_path(prefix)
    return _normalize_path(f"{prefix.rstrip('/')}/{path.lstrip('/')}")


def _router_identities(
    router: Any,
    *,
    included_prefix: str = "",
    visited: set[int] | None = None,
) -> Iterator[_RouteIdentity]:
    visited = set() if visited is None else visited
    router_identity = id(router)
    if router_identity in visited:
        raise ConfigError("HTTP router inclusion cycle is invalid")
    visited.add(router_identity)
    router_prefix = getattr(router, "prefix", "")
    if not isinstance(router_prefix, str):
        router_prefix = ""
    try:
        for route in router.routes:
            original_router = getattr(route, "original_router", None)
            include_context = getattr(route, "include_context", None)
            context_prefix = getattr(include_context, "prefix", None)
            if original_router is not None and isinstance(context_prefix, str):
                yield from _router_identities(
                    original_router,
                    included_prefix=_join_route_path(included_prefix, context_prefix),
                    visited=visited,
                )
                continue

            path = getattr(route, "path", None)
            if isinstance(route, Mount) and isinstance(path, str):
                mount_prefix = _join_route_path(
                    _join_route_path(included_prefix, router_prefix), path
                )
                yield _RouteIdentity(
                    None, _join_route_path(mount_prefix, "{path:path}")
                )
                continue

            methods = getattr(route, "methods", None)
            if not isinstance(path, str) or methods is None:
                continue
            for method in methods:
                yield _RouteIdentity(
                    method.upper(), _join_route_path(included_prefix, path)
                )
    finally:
        visited.remove(router_identity)


def _claim_route(identity: _RouteIdentity, occupied: list[_RouteIdentity]) -> None:
    for existing in occupied:
        methods_overlap = (
            existing.method is None
            or identity.method is None
            or existing.method == identity.method
        )
        if methods_overlap and _paths_overlap(existing.path, identity.path):
            method = identity.method or "*"
            raise ConfigError(f"HTTP route is duplicated: {method} {identity.path}")
    occupied.append(identity)


def _register_application_routers(
    app: FastAPI,
    routers: Iterable[APIRouter],
    occupied: list[_RouteIdentity],
) -> None:
    for router in routers:
        for identity in _router_identities(router):
            _claim_route(identity, occupied)
        app.include_router(router)


def _register_module_routes(
    app: FastAPI,
    kernel: Kernel,
    occupied: list[_RouteIdentity],
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
            _claim_route(_RouteIdentity(method, _normalize_path(spec.path)), occupied)
        app.add_api_route(
            spec.path,
            _build_endpoint(handler, spec),
            methods=list(spec.methods),
            status_code=spec.status_code,
            response_model=(
                SuccessEnvelope[spec.response_schema]  # type: ignore[name-defined]
                if spec.response_schema is not None
                else None
            ),
            tags=list(spec.tags) or [module_name],
        )


async def _stop_kernel(app: FastAPI, kernel: Kernel) -> None:
    try:
        shutdown_errors = await kernel.stop()
    except asyncio.CancelledError:
        logger.error("kernel_shutdown_failed")
        raise
    except Exception:
        logger.error("kernel_shutdown_failed")
        raise RuntimeError("kernel_shutdown_failed") from None
    app.state.shutdown_errors = shutdown_errors
    if shutdown_errors:
        logger.error("kernel_shutdown_failed")
        raise RuntimeError("kernel_shutdown_failed") from None


def create_app(
    *,
    settings: ApplicationSettings,
    kernel: Kernel,
    metrics: MetricsRegistry | None = None,
    routers: Iterable[APIRouter] = (),
    health_provider: Callable[[], dict[str, Any]] | None = None,
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
            await _stop_kernel(app, kernel)

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
            health = (
                health_provider()
                if health_provider is not None
                else kernel.health_status()
            )
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
        agent_runs = health.get("agent_runs")
        if isinstance(agent_runs, dict):
            metrics.set_component_health(
                "service",
                "agent_runs",
                healthy=agent_runs.get("status") == "healthy",
            )
        return success_response(health)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(content=metrics.render(), media_type=metrics.content_type)

    occupied = list(_router_identities(app))
    _register_application_routers(app, routers, occupied)
    try:
        _register_module_routes(app, kernel, occupied)
    except ModuleDependencyError:
        raise ConfigError("Invalid module HTTP route declaration") from None

    app.add_middleware(
        RequestBodyLimitMiddleware,
        limit=settings.server.max_request_body_bytes,
    )
    app.add_middleware(
        RequestTimeoutMiddleware,
        timeout_seconds=settings.server.request_timeout_seconds,
        max_response_buffer_bytes=settings.server.max_response_buffer_bytes,
        metrics=metrics,
    )
    if settings.cors.enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors.allow_origins,
            allow_methods=settings.cors.allow_methods,
            allow_headers=settings.cors.allow_headers,
        )
    app.add_middleware(
        RequestMetricsMiddleware,
        metrics=metrics,
    )
    if settings.logging.request_log_enabled:
        app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestContextMiddleware)
    return app
