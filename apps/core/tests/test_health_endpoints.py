"""Tests for production-style health and readiness probes."""

from __future__ import annotations

import logging

import pytest


class _RedisHealthy:
    def ping(self) -> bool:
        return True


class _RedisFailing:
    def ping(self) -> bool:
        raise RuntimeError("redis unavailable")


@pytest.mark.django_db
def test_health_returns_200(client):
    """GET /health/ returns liveness payload with no dependency checks."""
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "cart-system"}


@pytest.mark.django_db
def test_ready_returns_200_when_db_and_redis_are_healthy(client, monkeypatch):
    """GET /ready/ returns 200 when PostgreSQL and Redis checks pass."""
    monkeypatch.setattr("apps.core.views.get_redis_client", lambda: _RedisHealthy())
    response = client.get("/ready/")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["service"] == "cart-system"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"


@pytest.mark.django_db
def test_ready_returns_503_when_redis_check_fails(client, monkeypatch):
    """GET /ready/ returns 503 when Redis ping fails."""
    monkeypatch.setattr("apps.core.views.get_redis_client", lambda: _RedisFailing())
    response = client.get("/ready/")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["service"] == "cart-system"
    assert body["checks"]["postgres"] == "ok"
    assert "redis unavailable" in body["checks"]["redis"]


@pytest.mark.django_db
def test_schema_includes_health_and_ready_endpoints(client):
    """Swagger/OpenAPI schema exposes /health/ and /ready/ endpoints."""
    response = client.get("/api/schema/")
    assert response.status_code == 200
    schema_text = response.content.decode("utf-8")
    assert "/health/:" in schema_text
    assert "/ready/:" in schema_text


@pytest.mark.django_db
def test_ready_logs_dependency_failure_with_request_id(client, caplog, monkeypatch):
    """When Redis fails, an ERROR log is emitted with action=readiness.dependency_failed.

    The log record must also carry a non-empty request_id so the failure is
    traceable back to the specific probe invocation in a log aggregation tool.
    """
    monkeypatch.setattr("apps.core.views.get_redis_client", lambda: _RedisFailing())

    custom_rid = "readiness-trace-99"
    with caplog.at_level(logging.ERROR, logger="apps.core.views"):
        response = client.get("/ready/", HTTP_X_REQUEST_ID=custom_rid)

    assert response.status_code == 503

    failure_records = [
        r for r in caplog.records
        if getattr(r, "action", None) == "readiness.dependency_failed"
    ]
    assert failure_records, (
        "Expected at least one ERROR log with action='readiness.dependency_failed'"
    )
    for record in failure_records:
        assert record.levelno == logging.ERROR
        request_id = getattr(record, "request_id", None)
        assert request_id and request_id != "-", (
            f"request_id must be bound on readiness failure records, got {request_id!r}"
        )
