"""Shared RFC 7807 problem+json response helpers.

These helpers are shared between ``apps.order.views`` (checkout) and
``apps.cart.views`` (cart action endpoints).  Centralising them here
avoids duplicated code and ensures a consistent problem+json shape
(PROJECT_SPEC §5.4 / §6.4) across every endpoint.

Public API
----------
``problem(type_, title, status, detail, **extra) -> Response``
    Build a minimal RFC 7807 problem+json ``Response``.

``validation_problem(errors) -> Response``
    Build a RFC 7807 400 response for serializer validation failures,
    embedding the field-level ``errors`` dict as an extra member.

``map_exception(exc) -> Response``
    Translate a domain exception into an RFC 7807 ``Response``.
    Re-raises anything it cannot map so Django's 500 handler catches it.
    Logs unexpected exceptions at ERROR level (with ``request_id``) before
    re-raising so the traceback is always correlatable.
"""

from __future__ import annotations

import logging

from rest_framework.response import Response

logger = logging.getLogger(__name__)

from apps.core.exceptions import (
    IdempotencyConflict,
    IdempotencyInProgress,
    LockNotAcquired,
)
from apps.coupon.exceptions import CouponDomainError
from apps.order.exceptions import (
    AddressNotFound,
    CartAlreadyCheckedOut,
    CartEmpty,
    CartNotFound,
    CartStaleVersion,
    OrderDomainError,
    PaymentMethodInvalid,
    ProductOutOfStock,
)
from apps.payment.exceptions import PaymentDomainError, UnsupportedGateway


_BASE_URI = "https://cart-system.local/problems"


def problem(
    type_: str,
    title: str,
    status: int,
    detail: str,
    **extra,
) -> Response:
    """Build a minimal RFC 7807 problem+json response."""
    body: dict = {
        "type": f"{_BASE_URI}/{type_}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    body.update(extra)
    return Response(body, status=status, content_type="application/problem+json")


def validation_problem(errors: dict) -> Response:
    """Build a RFC 7807 400 response for serializer validation failures.

    Embeds the DRF field-level ``errors`` dict as an extra member so clients
    can pinpoint exactly which field(s) failed, while the outer envelope
    remains a fully-compliant RFC 7807 problem+json body.
    """
    return problem(
        "validation/invalid-input",
        "Validation error",
        400,
        "The request body contains invalid data.",
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Exception → HTTP status + title look-up table
# ---------------------------------------------------------------------------

_EXCEPTION_MAP: dict[type, tuple[int, str]] = {
    # 404 Not Found
    CartNotFound: (404, "Cart not found"),
    AddressNotFound: (404, "Address not found"),
    PaymentMethodInvalid: (404, "Payment method not found"),
    # 409 Conflict
    CartStaleVersion: (409, "Cart was modified concurrently — retry"),
    CartAlreadyCheckedOut: (409, "Cart is already checked out"),
    LockNotAcquired: (409, "A concurrent checkout is already in progress"),
    IdempotencyConflict: (409, "Idempotency-Key reused with a different request"),
    IdempotencyInProgress: (409, "Request with this Idempotency-Key is in progress"),
    # 422 Unprocessable (business-rule failures)
    CartEmpty: (422, "Cart is empty"),
    ProductOutOfStock: (422, "A product is out of stock"),
}


def map_exception(exc: Exception) -> Response:
    """Convert a domain exception to an RFC 7807 problem+json ``Response``.

    Re-raises any exception it cannot map so Django's 500 handler catches it.
    """
    exc_type = type(exc)

    if exc_type in _EXCEPTION_MAP:
        status, title = _EXCEPTION_MAP[exc_type]
        type_slug = getattr(exc, "type", "error/unknown")
        return problem(type_slug, title, status, str(exc))

    if isinstance(exc, CouponDomainError):
        return problem(exc.type, "Coupon validation failed", 422, exc.detail)

    if isinstance(exc, UnsupportedGateway):
        return problem(exc.type, "Payment gateway unavailable", 422, exc.detail)

    if isinstance(exc, PaymentDomainError):
        return problem(exc.type, "Payment error", 422, exc.detail)

    if isinstance(exc, OrderDomainError):
        return problem(exc.type, "Checkout error", 422, exc.detail)

    logger.exception(
        "error.unhandled",
        extra={"action": "error.unhandled", "exc_type": type(exc).__name__},
    )
    raise exc
