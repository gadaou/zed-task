"""Coupon domain exceptions.

Implements the coupon entries of the PROJECT_SPEC §2 error taxonomy:

    coupon/not-found
    coupon/expired
    coupon/limit-reached
    coupon/constraint-failed

These exceptions are raised by ``CouponValidator`` and ``CouponService`` and
will be translated to RFC 7807 problem+json by the custom DRF exception
handler that lands with ``apps.core`` (PROJECT_SPEC §6.4).  Until that handler
exists each exception still carries a stable ``type`` attribute so view-layer
code (and tests) can route on it without string-matching messages.

Design rules:
- Every exception inherits from ``CouponDomainError`` so a single ``except``
  clause in the view layer covers the whole subsystem.
- ``type`` matches the spec taxonomy verbatim, including the ``coupon/``
  prefix.  Non-spec error types are not invented here — if a new failure mode
  appears the spec is updated first (PROJECT_SPEC "How to read this spec").
- ``CouponConstraintFailed`` carries the offending constraint key so the
  serializer can surface "which rule failed" to the client without leaking
  the rule's logic.
"""

from __future__ import annotations

from typing import Any


class CouponDomainError(Exception):
    """Base class for every coupon-related domain error.

    Subclasses set the class-level ``type`` attribute to the stable URI used
    in the RFC 7807 problem+json response.  ``detail`` is a short, safe-for-
    logs human-readable message; it must never carry PII or secrets.
    """

    type: str = "coupon/error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        self.detail = detail or self.__class__.__doc__ or ""
        # ``extra`` lets subclasses attach machine-readable context (e.g. the
        # constraint key, the limit) that the problem+json serializer can pick
        # up. We deliberately do NOT format it into the str() output to keep
        # logs stable; structured logging consumes ``self.extra`` directly.
        self.extra = extra
        super().__init__(self.detail)


class CouponNotFound(CouponDomainError):
    """The requested coupon code does not exist in the active tenant."""

    type = "coupon/not-found"


class CouponInactive(CouponDomainError):
    """The coupon exists but has been deactivated (``is_active=False``).

    Maps to ``coupon/expired`` in the spec taxonomy because clients do not
    distinguish "manually disabled" from "past its end date" — both surfaces
    as "this coupon can no longer be used".
    """

    type = "coupon/expired"


class CouponExpired(CouponDomainError):
    """The current time is outside the coupon's validity window.

    Covers both "starts_at is in the future" and "ends_at is in the past"
    cases — clients only need to know the coupon is unusable right now.
    """

    type = "coupon/expired"


class CouponLimitReached(CouponDomainError):
    """The coupon's global usage cap has been hit (``used_count >= usage_limit``)."""

    type = "coupon/limit-reached"


class CouponConstraintFailed(CouponDomainError):
    """A JSON-driven constraint rejected the apply attempt.

    The ``constraint_key`` attribute carries the offending key from
    ``coupon.constraints`` so the API layer can render
    ``"failed_constraint": "min_total"`` (or similar) without depending on the
    rule's internal validation logic.

    ``constraint_key="unknown"`` indicates the constraint dict contained a key
    that no validator handler is registered for — surfaced as a hard failure
    rather than silently allowed, so that typos in admin tooling cannot
    accidentally widen a coupon's eligibility.
    """

    type = "coupon/constraint-failed"

    def __init__(self, constraint_key: str, detail: str = "", **extra: Any) -> None:
        self.constraint_key = constraint_key
        super().__init__(detail or f"constraint '{constraint_key}' failed", **extra)


class CouponAlreadyApplied(CouponDomainError):
    """The coupon is already applied to this cart.

    Surfaced when the unique constraint ``uq_cartcoupon_cart_coupon`` would be
    violated by a re-apply.  Intentionally a 409-class error — the server
    state is consistent, the client is asking for an idempotency-breaking
    duplicate.
    """

    type = "coupon/already-applied"


class CouponStackingViolation(CouponDomainError):
    """The cart's stacking policy rejects this combination of coupons.

    Raised by ``CouponService`` when a new coupon would violate the active
    ``StackingPolicy`` (e.g. a second PERCENTAGE coupon under the default
    "one of each discount_type" policy).

    Carries the policy name and the conflicting ``existing_coupon_id`` in
    ``extra`` so the API layer can render a helpful "you already have a
    percentage coupon applied" error without leaking implementation details.
    """

    type = "coupon/stacking-violation"
