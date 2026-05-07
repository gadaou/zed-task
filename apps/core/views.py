"""Core views — production-style health and readiness probes.

Endpoints:

* ``GET /health/`` — liveness only, no dependency checks.
* ``GET /ready/``  — readiness (PostgreSQL + Redis checks).

Compatibility aliases are kept in ``apps/core/urls.py``:

* ``/healthz``
* ``/readyz``
"""

from __future__ import annotations

import logging

from django.db import connections
from django.db.utils import Error as DatabaseError
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.metrics import incr
from apps.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def _check_postgres() -> tuple[bool, str]:
    """Return ``(ok, detail)`` for PostgreSQL connectivity."""
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, "ok"
    except DatabaseError as exc:
        return False, str(exc)


def _check_redis() -> tuple[bool, str]:
    """Return ``(ok, detail)`` for Redis connectivity."""
    try:
        redis_client = get_redis_client()
        redis_client.ping()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - readiness should catch all client errors
        return False, str(exc)


@extend_schema(
    tags=["Health"],
    summary="Liveness probe",
    description=(
        "Lightweight process liveness check. Does not touch PostgreSQL or Redis. "
        "Use for container liveness probes."
    ),
    responses={
        200: OpenApiResponse(
            description="Service process is alive.",
            response={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "example": "ok"},
                    "service": {"type": "string", "example": "cart-system"},
                },
                "required": ["status", "service"],
            },
        )
    },
)
@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def health(_request: Request) -> Response:
    """Liveness endpoint — no external dependency checks."""
    return Response({"status": "ok", "service": "cart-system"}, status=status.HTTP_200_OK)


@extend_schema(
    tags=["Health"],
    summary="Readiness probe",
    description=(
        "Dependency readiness check. Verifies PostgreSQL and Redis connectivity. "
        "Returns 200 when all dependencies are healthy, otherwise 503."
    ),
    responses={
        200: OpenApiResponse(
            description="All dependencies are healthy.",
            response={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "example": "ready"},
                    "service": {"type": "string", "example": "cart-system"},
                    "checks": {
                        "type": "object",
                        "properties": {
                            "postgres": {"type": "string", "example": "ok"},
                            "redis": {"type": "string", "example": "ok"},
                        },
                    },
                },
                "required": ["status", "service", "checks"],
            },
        ),
        503: OpenApiResponse(
            description="One or more dependencies are unavailable.",
            response={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "example": "unavailable"},
                    "service": {"type": "string", "example": "cart-system"},
                    "checks": {
                        "type": "object",
                        "properties": {
                            "postgres": {"type": "string", "example": "ok"},
                            "redis": {"type": "string", "example": "Error 111 connecting..."},
                        },
                    },
                },
                "required": ["status", "service", "checks"],
            },
        ),
    },
)
@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def ready(_request: Request) -> Response:
    """Readiness endpoint — validates PostgreSQL and Redis connectivity."""
    db_ok, db_detail = _check_postgres()
    redis_ok, redis_detail = _check_redis()

    checks = {
        "postgres": "ok" if db_ok else db_detail,
        "redis": "ok" if redis_ok else redis_detail,
    }
    all_ok = db_ok and redis_ok

    if not db_ok:
        logger.error(
            "readiness.dependency_failed",
            extra={
                "action": "readiness.dependency_failed",
                "component": "postgres",
                "detail": db_detail,
            },
        )
        incr("readiness.dependency_failed", component="postgres")

    if not redis_ok:
        logger.error(
            "readiness.dependency_failed",
            extra={
                "action": "readiness.dependency_failed",
                "component": "redis",
                "detail": redis_detail,
            },
        )
        incr("readiness.dependency_failed", component="redis")

    return Response(
        {
            "status": "ready" if all_ok else "unavailable",
            "service": "cart-system",
            "checks": checks,
        },
        status=status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
