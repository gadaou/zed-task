"""Request-Id correlation middleware.

``RequestIdMiddleware`` ensures every HTTP request carries a stable, unique
identifier throughout its lifetime:

- If the client sends ``X-Request-Id``, that value is preserved (after a
  safety sanity check — max 128 chars, non-empty after strip).
- If the header is absent or invalid, a new UUID4 hex string is generated.
- The resolved id is:
  1. Stored on ``request.request_id`` for view/middleware access.
  2. Bound into the ``ContextVar`` in ``apps.core.context`` so every log
     record emitted downstream (service layer, Celery preamble) carries it.
  3. Echoed back in the ``X-Request-Id`` response header so clients can
     correlate their request with server-side log lines.

Placement in ``MIDDLEWARE``
---------------------------
This middleware must appear immediately after ``SecurityMiddleware`` (and
before ``TenantMiddleware``) so that even tenant-resolution failures and
early 400/403 error responses carry a meaningful ``X-Request-Id``.

``X-User-Id`` propagation
--------------------------
The ``X-User-Id`` header (if present) is also bound into
``apps.core.context.user_id_ctx`` at this point so that log records emitted
by tenant-resolution or authentication middleware carry the user identifier
without requiring an additional filter on the view layer.
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from django.http import HttpRequest, HttpResponse

from apps.core.context import bind_request_context, reset_request_context

_logger = logging.getLogger(__name__)

_MAX_REQUEST_ID_LEN = 128


def _sanitise_request_id(raw: str | None) -> str | None:
    """Return ``raw`` if it looks usable, otherwise ``None``.

    Usable means: non-empty after stripping whitespace and not longer than
    ``_MAX_REQUEST_ID_LEN`` characters (to prevent log-injection via
    oversized header values).
    """
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > _MAX_REQUEST_ID_LEN:
        return None
    return cleaned


class RequestIdMiddleware:
    """WSGI/ASGI middleware that resolves and propagates the ``X-Request-Id``.

    Must be placed early in ``MIDDLEWARE`` — immediately after
    ``SecurityMiddleware`` and before ``TenantMiddleware`` — so all
    downstream log lines carry a request id.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # ------------------------------------------------------------------
        # Resolve request id: prefer client-supplied, fall back to generated.
        # ------------------------------------------------------------------
        raw_request_id = request.META.get("HTTP_X_REQUEST_ID")
        request_id = _sanitise_request_id(raw_request_id) or uuid.uuid4().hex

        # ------------------------------------------------------------------
        # Resolve user id from the X-User-Id header (best-effort; no error
        # if absent — that is enforced by views that require it).
        # ------------------------------------------------------------------
        raw_user_id = request.META.get("HTTP_X_USER_ID", "").strip() or None

        # ------------------------------------------------------------------
        # Attach to the request object for direct access in views.
        # ------------------------------------------------------------------
        request.request_id = request_id  # type: ignore[attr-defined]

        # ------------------------------------------------------------------
        # Bind into ContextVars so log records produced anywhere downstream
        # (service layer, signal handlers) carry the same identifiers.
        # Reset in finally so the context is clean for the next request on
        # this worker thread/coroutine.
        # ------------------------------------------------------------------
        token = bind_request_context(request_id=request_id, user_id=raw_user_id)
        try:
            response = self.get_response(request)
        finally:
            reset_request_context(token)

        # ------------------------------------------------------------------
        # Echo the resolved request id back to the client.
        # ------------------------------------------------------------------
        response["X-Request-Id"] = request_id

        _logger.debug(
            "http.request",
            extra={
                "action": "http.request",
                "method": request.method,
                "path": request.path_info,
                "status": response.status_code,
                # Include request_id directly so it is visible on the record
                # even when the RequestContextFilter has not yet run (e.g.
                # when captured by pytest caplog before handler dispatch).
                "request_id": request_id,
            },
        )
        return response
