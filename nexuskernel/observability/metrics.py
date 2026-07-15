"""Isolated Prometheus metrics with bounded response caching."""

from __future__ import annotations

import re
import threading
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest


class MetricsRegistry:
    content_type = CONTENT_TYPE_LATEST

    def __init__(self, prefix: str, *, cache_ttl_seconds: float = 5.0) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", prefix) is None:
            raise ValueError("metrics prefix must be a stable lowercase identifier")
        if cache_ttl_seconds <= 0:
            raise ValueError("metrics cache TTL must be positive")
        self._cache_ttl_seconds = cache_ttl_seconds
        self._registry = CollectorRegistry(auto_describe=True)
        self._http_requests = Counter(
            f"{prefix}_http_requests",
            "HTTP requests by method, declared route, and status.",
            ("method", "route", "status"),
            registry=self._registry,
        )
        self._http_duration = Histogram(
            f"{prefix}_http_request_duration_seconds",
            "HTTP request duration by method and declared route.",
            ("method", "route"),
            registry=self._registry,
        )
        self._component_health = Gauge(
            f"{prefix}_component_health",
            "Component health where 1 is healthy and 0 is degraded.",
            ("kind", "component"),
            registry=self._registry,
        )
        self._cache: bytes | None = None
        self._cache_expires_at = 0.0
        self._lock = threading.Lock()

    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None
            self._cache_expires_at = 0.0

    def record_http(
        self, method: str, route: str, status: int, duration_seconds: float
    ) -> None:
        if duration_seconds < 0:
            raise ValueError("HTTP duration cannot be negative")
        self._http_requests.labels(method=method, route=route, status=str(status)).inc()
        self._http_duration.labels(method=method, route=route).observe(duration_seconds)
        self._invalidate()

    def set_component_health(self, kind: str, component: str, *, healthy: bool) -> None:
        self._component_health.labels(kind=kind, component=component).set(
            1 if healthy else 0
        )
        self._invalidate()

    def render(self) -> bytes:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now < self._cache_expires_at:
                return self._cache
            self._cache = generate_latest(self._registry)
            self._cache_expires_at = now + self._cache_ttl_seconds
            return self._cache
