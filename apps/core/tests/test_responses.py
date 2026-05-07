"""Unit tests for ``apps.core.responses``.

Covers the two public helpers added in the error-handling audit:

``validation_problem(errors)``
    Must return a fully-compliant RFC 7807 problem+json response with a URI
    ``type``, ``title``, ``status`` == 400, and the DRF ``errors`` dict
    embedded as an extra member.

``map_exception(exc)`` — unhandled path
    When passed an exception that is not in ``_EXCEPTION_MAP`` and is not a
    recognised domain error, it must:
    - re-raise the original exception so Django's 500 handler can catch it, and
    - log an ERROR record with ``action == "error.unhandled"`` *before* raising
      so the ``request_id`` ContextVar (bound by ``RequestIdMiddleware``) is
      still active at log time.
"""

from __future__ import annotations

import logging

import pytest

from apps.core.responses import map_exception, validation_problem


# ---------------------------------------------------------------------------
# validation_problem()
# ---------------------------------------------------------------------------


def test_validation_problem_status_code():
    resp = validation_problem({"field": ["This field is required."]})
    assert resp.status_code == 400


def test_validation_problem_content_type():
    resp = validation_problem({"field": ["error"]})
    assert resp.content_type == "application/problem+json"


def test_validation_problem_type_is_uri():
    resp = validation_problem({"field": ["error"]})
    body = resp.data
    assert body["type"].startswith(
        "https://"
    ), f"'type' must be a URI, got {body['type']!r}"
    assert "validation/invalid-input" in body["type"]


def test_validation_problem_has_title_and_status():
    resp = validation_problem({"field": ["error"]})
    body = resp.data
    assert "title" in body, "RFC 7807 body must include 'title'"
    assert body["status"] == 400, "'status' must mirror the HTTP status code"


def test_validation_problem_embeds_errors():
    errors = {"name": ["This field may not be blank."], "qty": ["Must be >= 1."]}
    resp = validation_problem(errors)
    body = resp.data
    assert "errors" in body, "Validation body must contain the 'errors' member"
    assert body["errors"] == errors


# ---------------------------------------------------------------------------
# map_exception() — unhandled / re-raise path
# ---------------------------------------------------------------------------


def test_map_exception_reraises_unknown_exception(caplog):
    exc = RuntimeError("something totally unexpected")
    with pytest.raises(RuntimeError, match="something totally unexpected"):
        with caplog.at_level(logging.ERROR, logger="apps.core.responses"):
            map_exception(exc)


def test_map_exception_logs_before_reraise(caplog):
    """An ERROR record with action='error.unhandled' must be emitted before re-raise."""
    exc = RuntimeError("db connection lost")
    with pytest.raises(RuntimeError):
        with caplog.at_level(logging.ERROR, logger="apps.core.responses"):
            map_exception(exc)

    unhandled = [
        r for r in caplog.records if getattr(r, "action", None) == "error.unhandled"
    ]
    assert unhandled, (
        "Expected at least one ERROR log record with action='error.unhandled' "
        f"before re-raise. Records: {[(r.name, r.levelname) for r in caplog.records]}"
    )


def test_map_exception_log_includes_exc_type(caplog):
    exc = ValueError("bad value")
    with pytest.raises(ValueError):
        with caplog.at_level(logging.ERROR, logger="apps.core.responses"):
            map_exception(exc)

    matching = [
        r for r in caplog.records if getattr(r, "exc_type", None) == "ValueError"
    ]
    assert matching, "log record must carry exc_type='ValueError'"
