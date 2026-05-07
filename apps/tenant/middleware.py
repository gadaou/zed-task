"""TenantMiddleware — resolves the active tenant for every request.

Implements PROJECT_SPEC §4.2:
    "TenantMiddleware runs before authentication-derived logic, resolves the
    tenant from a stable signal … and writes it into a ContextVar."

Resolution signal: ``X-Tenant-Domain`` request header.

Response codes (§2 error taxonomy):
    400  X-Tenant-Domain header missing on a non-exempt path.
    404  Domain does not match any tenant row.
    403  Tenant exists but ``is_active=False``.

Exempt paths (configured via ``settings.TENANT_EXEMPT_PATHS``) bypass
resolution entirely.  This covers health probes, schema endpoints, and the
Django admin — which manages tenants directly and handles its own scoping.

ContextVar safety:
    ``set_current_tenant`` returns a reset token that is stored and applied in
    a ``finally`` block, so even if the view raises an unhandled exception the
    context is restored before the next request is processed on the same
    worker/coroutine.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from apps.tenant.context import reset_current_tenant, set_current_tenant
from apps.tenant.exceptions import TenantDisabled, TenantNotFound
from apps.tenant.models import Tenant

logger = logging.getLogger(__name__)

_HEADER = "HTTP_X_TENANT_DOMAIN"
_DEFAULT_EXEMPT = [
    "/admin/",
    "/healthz",
    "/readyz",
    "/api/schema/",
    "/api/docs/",
    "/api/redoc/",
]

_BASE_URI = "https://cart-system.local/problems"
_TITLES: dict[str, str] = {
    "tenant/missing-header": "X-Tenant-Domain header is required",
    "tenant/not-found": "Tenant not found",
    "tenant/disabled": "Tenant is disabled",
}


def _json_error(status: int, error_type: str, detail: str) -> HttpResponse:
    body = json.dumps({
        "type": f"{_BASE_URI}/{error_type}",
        "title": _TITLES.get(error_type, "Tenant error"),
        "status": status,
        "detail": detail,
    })
    return HttpResponse(body, status=status, content_type="application/problem+json")


def _is_exempt(path: str) -> bool:
    exempt_paths: list[str] = getattr(settings, "TENANT_EXEMPT_PATHS", _DEFAULT_EXEMPT)
    return any(path.startswith(prefix) for prefix in exempt_paths)


class TenantMiddleware:
    """WSGI/ASGI middleware that resolves and attaches the active tenant.

    Must be placed early in ``MIDDLEWARE`` — immediately after
    ``SecurityMiddleware`` — so that all downstream middleware and views
    receive a populated ``request.tenant``.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if _is_exempt(request.path_info):
            # ADR-NOTE: exempt paths (health probes, admin, schema) do not
            # require a tenant header; we skip resolution entirely so those
            # routes never return a spurious 400/404.
            request.tenant = None  # type: ignore[attr-defined]
            return self.get_response(request)

        domain = request.META.get(_HEADER)
        if not domain:
            return _json_error(
                400,
                "tenant/missing-header",
                "X-Tenant-Domain header is required.",
            )

        try:
            # Tenant uses Django's default manager (it is NOT a TenantAwareModel),
            # so a plain .get() is the correct call here.
            tenant = Tenant.objects.get(domain=domain)
        except Tenant.DoesNotExist:
            logger.warning("tenant.not_found domain=%s", domain)
            exc = TenantNotFound(f"No tenant found for domain '{domain}'.")
            return _json_error(exc.http_status, exc.type, exc.detail)

        if not tenant.is_active:
            logger.warning("tenant.disabled tenant_id=%s domain=%s", tenant.id, domain)
            exc = TenantDisabled(f"Tenant '{domain}' is disabled.")
            return _json_error(exc.http_status, exc.type, exc.detail)

        request.tenant = tenant  # type: ignore[attr-defined]
        token = set_current_tenant(tenant)
        try:
            return self.get_response(request)
        finally:
            reset_current_tenant(token)
