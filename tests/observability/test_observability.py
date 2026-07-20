from __future__ import annotations

import json
import logging

import pytest
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


@pytest.mark.parametrize(
    "ttl", [True, 0.0, -1.0, float("nan"), float("inf"), float("-inf")]
)
def test_metrics_registry_rejects_non_positive_finite_cache_ttl(ttl: object) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        MetricsRegistry("sample", cache_ttl_seconds=ttl)  # type: ignore[arg-type]


def test_metrics_mutation_invalidates_cache() -> None:
    metrics = MetricsRegistry("sample", cache_ttl_seconds=10)
    first = metrics.render()
    metrics.record_http("GET", "/health/live", 200, 0.01)
    second = metrics.render()

    assert first is not second
    assert b"/health/live" in second


def test_agent_runs_metrics_use_only_bounded_operational_labels() -> None:
    metrics = MetricsRegistry("sample", cache_ttl_seconds=10)

    metrics.set_agent_runs_runtime(
        queue_depth=2,
        active_phases=1,
        waiting_actions=3,
        cleanup_debt=1,
        runs_by_status={"queued": 2, "running": 1, "requires_action": 1},
    )
    metrics.record_agent_run_transition("running")
    metrics.record_agent_run_terminal("succeeded")
    metrics.record_agent_run_phase("succeeded", 0.25)
    metrics.record_agent_runs_capacity_rejection()

    rendered = metrics.render()
    assert b"sample_agent_runs_queue_depth 2.0" in rendered
    assert b"sample_agent_runs_active_phases 1.0" in rendered
    assert b"sample_agent_runs_waiting_actions 3.0" in rendered
    assert b"sample_agent_runs_cleanup_debt 1.0" in rendered
    assert b'sample_agent_runs_current{status="queued"} 2.0' in rendered
    assert (
        b'sample_agent_runs_status_transitions_total{status="running"} 1.0' in rendered
    )
    assert (
        b'sample_agent_runs_terminal_results_total{status="succeeded"} 1.0' in rendered
    )
    assert (
        b'sample_agent_runs_phase_duration_seconds_count{outcome="succeeded"} 1.0'
        in rendered
    )
    assert b"sample_agent_runs_capacity_rejections_total 1.0" in rendered
    assert b"principal" not in rendered
    assert b"run_id" not in rendered
    assert b"session_id" not in rendered


@pytest.mark.parametrize(
    ("operation", "value"),
    [
        ("transition", "tenant-controlled-status"),
        ("terminal", "running"),
        ("phase", "provider-specific-result"),
    ],
)
def test_agent_runs_metrics_reject_unbounded_label_values(
    operation: str, value: str
) -> None:
    metrics = MetricsRegistry("sample")

    with pytest.raises(ValueError, match="Agent Runs"):
        if operation == "transition":
            metrics.record_agent_run_transition(value)
        elif operation == "terminal":
            metrics.record_agent_run_terminal(value)
        else:
            metrics.record_agent_run_phase(value, 0.1)


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


def test_asyncio_logging_does_not_format_task_or_exception_objects(
    capsys: object,
) -> None:
    configure_logging(level="ERROR", serialize=False, enqueue=False)
    private_detail = "token=async-task-private-detail"

    logging.getLogger("asyncio").error(
        "Task exception was never retrieved: %r", RuntimeError(private_detail)
    )
    logging.getLogger("asyncio.base_events").error(
        "Future exception was never retrieved: %r", RuntimeError(private_detail)
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.out.count("asyncio_runtime_error") == 2
    assert private_detail not in captured.out


def test_adk_sdk_logging_is_fail_closed_without_formatting_content(
    capsys: object,
) -> None:
    configure_logging(level="DEBUG", serialize=False, enqueue=False)
    formatted = False
    exception_formatted = False

    class SensitiveValue:
        def __str__(self) -> str:
            nonlocal formatted
            formatted = True
            return "prompt=must-not-be-formatted"

    class SensitiveError(RuntimeError):
        def __str__(self) -> str:
            nonlocal exception_formatted
            exception_formatted = True
            return "model_response=must-not-be-formatted"

    logging.getLogger("google.adk.models").debug("request %s", SensitiveValue())
    logging.getLogger("google.genai.client").warning("response %s", SensitiveValue())
    logging.getLogger("google_adk.workflow").error("tool %s", SensitiveValue())
    try:
        raise SensitiveError
    except SensitiveError:
        logging.getLogger("google_genai.models").exception(
            "request %s", SensitiveValue()
        )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert not formatted
    assert not exception_formatted
    assert "adk_sdk_warning" in captured.out
    assert "adk_sdk_error" in captured.out
    assert "must-not-be-formatted" not in captured.out
