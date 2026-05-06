"""Core views — health checks and root API entrypoint.

* ``healthz`` — liveness, no dependencies.
* ``readyz``  — readiness, will check Postgres + Redis once those are wired.
"""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse


def healthz(_request: HttpRequest) -> JsonResponse:
    """Liveness probe — process is up."""
    return JsonResponse({"status": "ok"})


def readyz(_request: HttpRequest) -> JsonResponse:
    """Readiness probe — process is up and ready to serve traffic.

    Once Postgres and Redis are wired, this will perform a cheap query and a
    Redis ``PING``. For now it mirrors ``healthz``.
    """
    return JsonResponse({"status": "ready"})
