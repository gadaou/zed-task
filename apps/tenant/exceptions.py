"""Tenant domain exceptions.

All exceptions inherit ``TenantError`` so the future problem+json exception
handler (PROJECT_SPEC §6.4) can identify and map them to RFC 7807 responses
with the §2 error-taxonomy ``type`` URIs in a single ``isinstance`` check.

Error codes match the taxonomy in PROJECT_SPEC §2:
    ``tenant/not-found``   → TenantNotFound
    ``tenant/disabled``    → TenantDisabled
    ``tenant/missing-header`` (HTTP 400) — raised by middleware, not here
    context-var unset      → TenantContextMissing (programmer/configuration error)
"""

from __future__ import annotations


class TenantError(Exception):
    """Base class for all tenant-related errors."""

    #: Short stable machine-readable code matching the §2 taxonomy.
    error_type: str = "tenant/error"
    default_detail: str = "A tenant error occurred."
    http_status: int = 500

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class TenantContextMissing(TenantError):
    """Raised when a tenant-aware queryset is evaluated with no tenant context.

    This is a programming / configuration error — service code forgot to run
    inside a ``tenant_context(...)`` block or the middleware was bypassed.
    Surfaces as a hard 500 so it is never silently swallowed.
    """

    error_type = "tenant/context-missing"
    default_detail = (
        "No tenant is set in the current execution context. "
        "Wrap the caller in tenant_context(tenant) or ensure TenantMiddleware runs."
    )
    http_status = 500


class TenantNotFound(TenantError):
    """Raised when a domain header value does not match any known tenant."""

    error_type = "tenant/not-found"
    default_detail = "No tenant found for the supplied domain."
    http_status = 404


class TenantDisabled(TenantError):
    """Raised when the resolved tenant exists but is marked inactive."""

    error_type = "tenant/disabled"
    default_detail = "This tenant is currently disabled."
    http_status = 403
