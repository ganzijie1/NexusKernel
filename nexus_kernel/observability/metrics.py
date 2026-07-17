"""Isolated Prometheus metrics with bounded response caching."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest


_AGENT_RUN_STATUSES = (
    "queued",
    "running",
    "requires_action",
    "canceling",
    "succeeded",
    "failed",
    "canceled",
    "expired",
)
_AGENT_RUN_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "expired"})
_AGENT_RUN_PHASE_OUTCOMES = frozenset(
    {"succeeded", "requires_action", "failed", "timed_out", "canceled", "expired"}
)


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
        self._background_failures = Counter(
            f"{prefix}_http_background_task_failures",
            "HTTP background task failures after response commit.",
            registry=self._registry,
        )
        self._agent_runs_queue_depth = Gauge(
            f"{prefix}_agent_runs_queue_depth",
            "Queued Agent Run phases.",
            registry=self._registry,
        )
        self._agent_runs_active_phases = Gauge(
            f"{prefix}_agent_runs_active_phases",
            "Agent Run phases currently executing.",
            registry=self._registry,
        )
        self._agent_runs_waiting_actions = Gauge(
            f"{prefix}_agent_runs_waiting_actions",
            "Public Agent Run actions awaiting a response.",
            registry=self._registry,
        )
        self._agent_runs_cleanup_debt = Gauge(
            f"{prefix}_agent_runs_cleanup_debt",
            "Agent Run and session cleanup operations not yet converged.",
            registry=self._registry,
        )
        self._agent_runs_current = Gauge(
            f"{prefix}_agent_runs_current",
            "Current retained Agent Runs by stable status.",
            ("status",),
            registry=self._registry,
        )
        self._agent_runs_status_transitions = Counter(
            f"{prefix}_agent_runs_status_transitions",
            "Agent Run transitions into each stable status.",
            ("status",),
            registry=self._registry,
        )
        self._agent_runs_terminal_results = Counter(
            f"{prefix}_agent_runs_terminal_results",
            "Terminal Agent Run results by stable status.",
            ("status",),
            registry=self._registry,
        )
        self._agent_runs_phase_duration = Histogram(
            f"{prefix}_agent_runs_phase_duration_seconds",
            "Agent Run phase duration by bounded outcome.",
            ("outcome",),
            registry=self._registry,
        )
        self._agent_runs_capacity_rejections = Counter(
            f"{prefix}_agent_runs_capacity_rejections",
            "Agent Run requests rejected because bounded capacity was exhausted.",
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

    def record_background_failure(self) -> None:
        self._background_failures.inc()
        self._invalidate()

    def set_agent_runs_runtime(
        self,
        *,
        queue_depth: int,
        active_phases: int,
        waiting_actions: int,
        cleanup_debt: int,
        runs_by_status: Mapping[str, int],
    ) -> None:
        values = {
            "queue_depth": queue_depth,
            "active_phases": active_phases,
            "waiting_actions": waiting_actions,
            "cleanup_debt": cleanup_debt,
        }
        for name, value in values.items():
            self._validate_agent_runs_count(name, value)
        unknown = set(runs_by_status).difference(_AGENT_RUN_STATUSES)
        if unknown:
            raise ValueError("Agent Runs status labels must use the stable allowlist")
        for status, value in runs_by_status.items():
            self._validate_agent_runs_count(f"status {status}", value)

        self._agent_runs_queue_depth.set(queue_depth)
        self._agent_runs_active_phases.set(active_phases)
        self._agent_runs_waiting_actions.set(waiting_actions)
        self._agent_runs_cleanup_debt.set(cleanup_debt)
        for status in _AGENT_RUN_STATUSES:
            self._agent_runs_current.labels(status=status).set(
                runs_by_status.get(status, 0)
            )
        self._invalidate()

    def record_agent_run_transition(self, status: str) -> None:
        if status not in _AGENT_RUN_STATUSES:
            raise ValueError("Agent Runs status labels must use the stable allowlist")
        self._agent_runs_status_transitions.labels(status=status).inc()
        self._invalidate()

    def record_agent_run_terminal(self, status: str) -> None:
        if status not in _AGENT_RUN_TERMINAL_STATUSES:
            raise ValueError("Agent Runs terminal labels must use the stable allowlist")
        self._agent_runs_terminal_results.labels(status=status).inc()
        self._invalidate()

    def record_agent_run_phase(self, outcome: str, duration_seconds: float) -> None:
        if outcome not in _AGENT_RUN_PHASE_OUTCOMES:
            raise ValueError("Agent Runs phase labels must use the stable allowlist")
        if duration_seconds < 0:
            raise ValueError("Agent Runs phase duration cannot be negative")
        self._agent_runs_phase_duration.labels(outcome=outcome).observe(
            duration_seconds
        )
        self._invalidate()

    def record_agent_runs_capacity_rejection(self) -> None:
        self._agent_runs_capacity_rejections.inc()
        self._invalidate()

    @staticmethod
    def _validate_agent_runs_count(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Agent Runs {name} must be a non-negative integer")

    def render(self) -> bytes:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now < self._cache_expires_at:
                return self._cache
            self._cache = generate_latest(self._registry)
            self._cache_expires_at = now + self._cache_ttl_seconds
            return self._cache
