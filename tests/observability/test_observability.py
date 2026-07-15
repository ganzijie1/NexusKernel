from __future__ import annotations

import json

from nexuskernel.observability import MetricsRegistry, redact_sensitive


def test_redact_sensitive_recurses_and_sanitizes_secret_shapes() -> None:
    value = {
        "authorization": "Bearer abcdef",
        "nested": {
            "api_key": "s" + "k-example-secret",
            "prompt": "private prompt",
            "safe": "hello",
        },
        "items": ["mongodb://user:password@db.example/data", "plain"],
    }

    redacted = redact_sensitive(value)
    serialized = json.dumps(redacted)

    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["nested"]["safe"] == "hello"
    assert "abcdef" not in serialized
    assert "s" + "k-example-secret" not in serialized
    assert "private prompt" not in serialized
    assert "password" not in serialized


def test_metrics_registry_is_isolated_and_renders_cached_prometheus_data() -> None:
    metrics = MetricsRegistry("sample", cache_ttl_seconds=10)

    metrics.record_http("POST", "/api/v1/chat", 200, 0.25)
    metrics.set_component_health("module", "chat", healthy=False)
    first = metrics.render()
    second = metrics.render()

    assert first is second
    assert b"sample_http_requests_total" in first
    assert b'method="POST"' in first
    assert b'route="/api/v1/chat"' in first
    assert b'status="200"' in first
    assert b'sample_component_health{component="chat",kind="module"} 0.0' in first


def test_metrics_mutation_invalidates_cache() -> None:
    metrics = MetricsRegistry("sample", cache_ttl_seconds=10)
    first = metrics.render()
    metrics.record_http("GET", "/health/live", 200, 0.01)
    second = metrics.render()

    assert first is not second
    assert b"/health/live" in second
