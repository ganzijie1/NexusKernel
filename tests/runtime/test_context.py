from __future__ import annotations

from nexus_kernel.runtime import bind_request_context, get_request_context


def test_request_context_is_scoped_and_restored() -> None:
    assert get_request_context() is None

    with bind_request_context("request-1", "trace-1") as outer:
        assert get_request_context() is outer
        assert outer.request_id == "request-1"
        assert outer.trace_id == "trace-1"

        with bind_request_context("request-2", "trace-2"):
            assert get_request_context() is not None
            assert get_request_context().request_id == "request-2"  # type: ignore[union-attr]

        assert get_request_context() is outer

    assert get_request_context() is None


def test_request_context_rejects_empty_or_multiline_identifiers() -> None:
    for request_id, trace_id in [("", "trace"), ("request", ""), ("a\nb", "trace")]:
        try:
            with bind_request_context(request_id, trace_id):
                raise AssertionError("invalid context should not be entered")
        except ValueError:
            pass
