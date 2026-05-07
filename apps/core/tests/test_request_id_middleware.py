"""Tests for RequestIdMiddleware — X-Request-Id correlation header.

Coverage:
- Response always contains X-Request-Id.
- Client-supplied X-Request-Id is echoed back unchanged.
- A generated id exists when the header is missing.
- The request_id attribute is set on the request object.
- A log record produced during the request carries the matching request_id.
"""

from __future__ import annotations

import logging
import uuid

import pytest


@pytest.mark.django_db
def test_request_id_returned_when_absent(client):
    """GET /health/ returns X-Request-Id even when the client does not send one."""
    response = client.get("/health/")
    assert response.status_code == 200
    assert (
        "X-Request-Id" in response
    ), "X-Request-Id header must be present in the response"
    rid = response["X-Request-Id"]
    assert rid, "X-Request-Id must not be empty"


@pytest.mark.django_db
def test_existing_request_id_preserved(client):
    """When the client sends X-Request-Id, the same value is echoed back."""
    custom_id = "my-trace-abc-123"
    response = client.get("/health/", HTTP_X_REQUEST_ID=custom_id)
    assert response.status_code == 200
    assert response["X-Request-Id"] == custom_id


@pytest.mark.django_db
def test_generated_request_id_is_valid_when_missing(client):
    """When X-Request-Id is absent, a UUID4 hex string is generated."""
    response = client.get("/health/")
    rid = response["X-Request-Id"]
    # The middleware generates uuid.uuid4().hex — 32 hex chars, no dashes.
    assert len(rid) == 32, f"Expected 32-char hex, got {rid!r}"
    # Must be parseable as a UUID (after inserting standard dashes).
    try:
        uuid.UUID(rid)
    except ValueError:
        pytest.fail(f"Generated X-Request-Id {rid!r} is not a valid UUID hex")


@pytest.mark.django_db
def test_oversized_request_id_is_replaced(client):
    """An X-Request-Id longer than 128 chars is discarded and a new one generated."""
    oversized = "x" * 200
    response = client.get("/health/", HTTP_X_REQUEST_ID=oversized)
    rid = response["X-Request-Id"]
    assert rid != oversized, "Oversized id should have been replaced"
    assert len(rid) == 32, "Replacement should be a fresh UUID hex"


class _RedisFailing:
    def ping(self) -> bool:
        raise RuntimeError("redis unavailable for log test")


@pytest.mark.django_db
def test_request_id_appears_in_log_records(client, caplog, monkeypatch):
    """A log record produced during a request carries the X-Request-Id value.

    We use the /ready/ endpoint with a failing Redis dependency because it logs
    at ERROR level — guaranteed to be captured regardless of the root logger
    level configured in test settings (WARNING).  The RequestContextFilter
    injects ``request_id`` onto the record from the ContextVar bound by the
    middleware.
    """
    monkeypatch.setattr("apps.core.views.get_redis_client", lambda: _RedisFailing())
    custom_id = "log-test-req-id-1234"

    with caplog.at_level(logging.ERROR, logger="apps.core.views"):
        response = client.get("/ready/", HTTP_X_REQUEST_ID=custom_id)

    assert response.status_code == 503
    # The RequestContextFilter injects request_id onto every LogRecord.
    matching = [
        r for r in caplog.records if getattr(r, "request_id", None) == custom_id
    ]
    assert matching, (
        f"Expected at least one log record with request_id={custom_id!r}. "
        f"Records: {[(r.name, getattr(r, 'request_id', '<missing>')) for r in caplog.records]}"
    )


@pytest.mark.django_db
def test_x_request_id_set_on_ready_endpoint(client):
    """X-Request-Id is returned on the /ready/ endpoint too."""
    response = client.get("/ready/")
    assert "X-Request-Id" in response
