from __future__ import annotations

import json
import logging

from loguru import logger

from nexus_kernel.observability import (
    MetricsRegistry,
    configure_logging,
    redact_sensitive,
)


def test_redact_sensitive_recurses_and_sanitizes_secret_shapes() -> None:
    value = {
        "authorization": "Bearer abcdef",  # security-test-fixture
        "nested": {
            "api_key": "s" + "k-example-secret",  # security-test-fixture
            "prompt": "private prompt",
            "base_url": "https://private-endpoint.invalid/v1",
            "safe": "hello",
        },
        "items": [
            "mongodb://user:password@db.example/data",  # security-test-fixture
            "plain",
        ],
    }

    redacted = redact_sensitive(value)
    serialized = json.dumps(redacted)

    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["nested"]["safe"] == "hello"
    assert "abcdef" not in serialized
    assert "s" + "k-example-secret" not in serialized
    assert "private prompt" not in serialized
    assert "private-endpoint" not in serialized
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


def test_standard_logging_is_redacted_and_drops_exception_details(
    capsys: object,
) -> None:
    configure_logging(level="INFO", serialize=False, enqueue=False)
    authorization = "Bearer " + "not-for-logs"  # security-test-fixture
    raw_detail = "upstream-" + "private-detail"

    try:
        raise RuntimeError(raw_detail)
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception(
            "provider failed with %s", authorization
        )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "provider failed with Bearer [REDACTED]" in captured.out
    assert "not-for-logs" not in captured.out
    assert raw_detail not in captured.out


def test_loguru_and_uvicorn_access_drop_untrusted_details(capsys: object) -> None:
    configure_logging(level="INFO", serialize=False, enqueue=False)
    private_detail = "detail-that-must-not-appear"

    try:
        raise RuntimeError(private_detail)
    except RuntimeError:
        logger.exception("provider failed secret={}", private_detail)
    logging.getLogger("uvicorn.access").info(
        "GET /health/live?prompt=%s", private_detail
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "secret=[REDACTED]" in captured.out
    assert "uvicorn_access" in captured.out
    assert private_detail not in captured.out
