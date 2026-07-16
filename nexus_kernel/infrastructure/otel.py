"""OpenTelemetry tracer adapter loaded only with the otel extra."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


class OpenTelemetryTracer:
    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

    @classmethod
    def create(cls, instrumentation_name: str) -> OpenTelemetryTracer:
        from opentelemetry import trace

        return cls(trace.get_tracer(instrumentation_name))

    @asynccontextmanager
    async def start_span(self, name: str) -> AsyncIterator[Any]:
        with self._tracer.start_as_current_span(name) as span:
            yield span
