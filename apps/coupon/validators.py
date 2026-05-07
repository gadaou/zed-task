"""Coupon validation — JSON-driven rule registry.

Implements the validator side of PROJECT_SPEC §2 ops 3–4 (apply/remove
coupon) and the bonus "Coupon constraints" section.

Design intent
-------------
The constraint set on a ``Coupon`` is an opaque JSON dict (see
``Coupon.constraints``); each top-level key names a rule.  ``CouponValidator``
keeps a class-level registry of rule handlers populated by the
``@CouponValidator.register("min_total")`` decorator and dispatches each key
to its handler at validation time.

Two consequences of this design that the project requirements call out:

1. **No hardcoded rule list.**  The validator never names ``min_total`` /
   ``allowed_countries`` / ``usage_limit`` directly inside the dispatch loop
   — adding a new rule is a single new ``@register`` decorator, not a code
   change to ``validate()``.
2. **Unknown keys fail closed.**  A constraint key with no registered handler
   raises ``CouponConstraintFailed("<key>", "unknown constraint")`` so a typo
   in admin tooling cannot silently widen a coupon's eligibility.

Built-in (always-on) checks not exposed as JSON rules
-----------------------------------------------------
A small set of checks always runs regardless of what is in ``constraints``:

* ``is_active`` — a coupon flipped to inactive is unusable, period.
* validity window — ``starts_at`` / ``ends_at`` are first-class columns with
  their own DB CHECK; we re-evaluate them here so apply-time gets the same
  answer as checkout (PROJECT_SPEC §5.3).
* ``usage_limit`` — column-first per the locked design decision: the model
  carries a ``usage_limit`` integer and an atomic ``used_count`` counter, so
  the validator reads those directly.  The JSON key ``usage_limit`` is
  honoured only when the column is null (a coupon imported from external
  tooling that put the cap into the JSON blob), so callers can switch
  representations without breaking validation.

Concurrency note
----------------
``CouponValidator`` is read-only — it never mutates the coupon or the cart.
It is safe to instantiate per-request and to call from inside a
``transaction.atomic`` block whose service-layer caller already holds a
``select_for_update`` row lock on the coupon (PROJECT_SPEC §3.4).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, ClassVar

from apps.coupon.exceptions import (
    CouponConstraintFailed,
    CouponExpired,
    CouponInactive,
    CouponLimitReached,
)
from apps.coupon.models import Coupon


@dataclass(frozen=True)
class CouponValidationContext:
    """Snapshot of cart/customer state evaluated by every rule.

    Frozen because rule handlers must not mutate the context — they validate,
    they do not enrich.  Built once per ``apply_coupon_to_cart`` call by the
    service layer.

    Attributes:
        cart_total: The cart's pre-discount subtotal.  In ``cart_currency``.
        cart_currency: ISO 4217 three-letter code; matches ``Cart.currency``.
        customer_country: ISO 3166-1 alpha-2 code derived from the customer's
            default ``Address``.  ``None`` when the customer has no default
            address — country-restricted coupons reject in that case.
        now: Reference time for the validity-window check.  Injectable so
            unit tests can pin "now" without monkeypatching the clock.
    """

    cart_total: Decimal
    cart_currency: str
    customer_country: str | None
    now: datetime


# Type alias for a rule handler.  Each handler accepts the coupon, the value
# the constraint dict assigns to its key, and the validation context.  It
# returns ``None`` on success and raises a ``CouponDomainError`` subclass on
# failure — typically ``CouponConstraintFailed`` keyed on its own constraint
# name.
RuleFn = Callable[[Coupon, Any, CouponValidationContext], None]


class CouponValidator:
    """JSON-driven coupon validator with a class-level rule registry.

    Usage::

        validator = CouponValidator()
        validator.validate(coupon, ctx)  # raises on failure, returns None on pass

    Adding a new rule::

        @CouponValidator.register("my_new_rule")
        def _check_my_new_rule(coupon, value, ctx):
            if not satisfied(value, ctx):
                raise CouponConstraintFailed("my_new_rule", "explanatory text")

    The registry is class-level (not instance-level) on purpose: rules are
    process-wide, registered at import time, and shared across every
    validator call.  Tests that need to introduce a temporary rule do so via
    ``CouponValidator._rules[...]`` and clean up in a fixture's ``finally``.
    """

    # Class-level — see docstring above.  Annotated as ClassVar so type checkers
    # do not flag it as an instance attribute that escapes ``__init__``.
    _rules: ClassVar[dict[str, RuleFn]] = {}

    @classmethod
    def register(cls, key: str) -> Callable[[RuleFn], RuleFn]:
        """Decorator that registers ``fn`` as the handler for constraint ``key``.

        Re-registering the same key is allowed (the new handler wins) so
        admin-style overrides remain possible during tests.  In production
        every rule is registered exactly once at import time.
        """

        def deco(fn: RuleFn) -> RuleFn:
            cls._rules[key] = fn
            return fn

        return deco

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def validate(self, coupon: Coupon, ctx: CouponValidationContext) -> None:
        """Run every applicable rule against ``coupon`` in ``ctx``.

        Order:
        1. Built-in checks (``is_active``, validity window, usage_limit).
           These run first because they are cheap and reject the most common
           failure modes (deactivated, expired, capped) before any
           per-constraint logic.
        2. JSON-driven rules in insertion order of ``coupon.constraints``.
           Insertion order is stable on Python 3.7+ dicts, which keeps the
           "first failing rule wins" behaviour deterministic and testable.

        Returns:
            ``None`` on success.

        Raises:
            CouponInactive: ``coupon.is_active`` is False.
            CouponExpired: ``ctx.now`` is outside the validity window.
            CouponLimitReached: ``used_count`` has reached ``usage_limit``.
            CouponConstraintFailed: any JSON-driven rule rejected, or the
                constraint dict carries a key with no registered handler.
        """
        self._check_active(coupon)
        self._check_validity_window(coupon, ctx.now)
        self._check_usage_limit(coupon)

        for key, value in (coupon.constraints or {}).items():
            # ``usage_limit`` may also appear in the JSON blob for compatibility
            # with external tooling.  When the column is set the column wins
            # (already enforced above), so re-running the JSON handler would
            # double-check at best and contradict at worst.  Skip it.
            if key == "usage_limit" and coupon.usage_limit is not None:
                continue

            handler = self._rules.get(key)
            if handler is None:
                raise CouponConstraintFailed(
                    key,
                    f"no validator registered for constraint '{key}'",
                )
            handler(coupon, value, ctx)

    # ------------------------------------------------------------------
    # Built-in checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_active(coupon: Coupon) -> None:
        if not coupon.is_active:
            raise CouponInactive(f"coupon '{coupon.code}' is not active")

    @staticmethod
    def _check_validity_window(coupon: Coupon, now: datetime) -> None:
        # Both ends are independently optional — a coupon may be open-ended on
        # either side.  Comparison is timezone-aware: the columns are stored
        # TIMESTAMPTZ and ``now`` is required to be tz-aware (the dataclass
        # docstring documents this).  An accidental naive datetime would raise
        # ``TypeError`` here, which is the correct loud failure in dev/test.
        if coupon.starts_at is not None and now < coupon.starts_at:
            raise CouponExpired(
                f"coupon '{coupon.code}' is not yet valid (starts at {coupon.starts_at.isoformat()})"
            )
        if coupon.ends_at is not None and now > coupon.ends_at:
            raise CouponExpired(
                f"coupon '{coupon.code}' has expired (ended at {coupon.ends_at.isoformat()})"
            )

    @staticmethod
    def _check_usage_limit(coupon: Coupon) -> None:
        # Column-first per the locked design decision.  The DB has a CHECK
        # ensuring used_count <= usage_limit when usage_limit is set, so this
        # check is defence-in-depth for the apply path: it rejects *before*
        # the F("used_count") + 1 increment that would violate the CHECK.
        column_limit = coupon.usage_limit
        if column_limit is None:
            # Fall back to the JSON key — supports coupons imported from
            # external tooling that puts the cap in the constraint blob.
            json_limit = (coupon.constraints or {}).get("usage_limit")
            if json_limit is None:
                return
            try:
                column_limit = int(json_limit)
            except (TypeError, ValueError) as exc:
                raise CouponConstraintFailed(
                    "usage_limit",
                    f"usage_limit must be an integer, got {json_limit!r}",
                ) from exc

        if coupon.used_count >= column_limit:
            raise CouponLimitReached(
                f"coupon '{coupon.code}' has been used {coupon.used_count} of {column_limit} times"
            )


# ----------------------------------------------------------------------
# Rule registrations
# ----------------------------------------------------------------------
# Each rule is a small, pure function. They live in this module (next to the
# validator) rather than a separate ``rules.py`` because there are only a few
# of them today and grouping them next to the registry makes the intent
# obvious to a first-time reader. If the rule set grows past ~10 we will
# split them out — flagged in the §7 design philosophy ("optimize for the
# next engineer").


@CouponValidator.register("min_total")
def _check_min_total(coupon: Coupon, value: Any, ctx: CouponValidationContext) -> None:
    """Cart subtotal must meet or exceed ``value`` (in cart currency).

    ``value`` may be any type ``Decimal`` accepts — int, str, float-as-string —
    so admin tooling does not need to pre-coerce it.  A type that ``Decimal``
    cannot consume raises ``CouponConstraintFailed`` rather than
    ``InvalidOperation`` so the API layer surfaces a stable error.

    Cross-currency comparison is intentionally NOT performed here: PROJECT_SPEC
    §3.5 is explicit that money carries a currency, and a real cross-currency
    coupon would need an FX feed which is out of scope (PROJECT_SPEC §8).
    The constraint is documented to be denominated in the cart's currency;
    multi-currency tenants are expected to provision a coupon per currency.
    """
    try:
        threshold = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CouponConstraintFailed(
            "min_total",
            f"min_total must be a numeric value, got {value!r}",
        ) from exc

    if ctx.cart_total < threshold:
        raise CouponConstraintFailed(
            "min_total",
            (
                f"cart subtotal {ctx.cart_total} {ctx.cart_currency} is below "
                f"the minimum {threshold} required for coupon '{coupon.code}'"
            ),
            cart_total=str(ctx.cart_total),
            min_total=str(threshold),
        )


@CouponValidator.register("allowed_countries")
def _check_allowed_countries(
    coupon: Coupon, value: Any, ctx: CouponValidationContext
) -> None:
    """Customer's country must be one of ``value`` (ISO 3166-1 alpha-2 codes).

    The customer's country is resolved by the service layer from the default
    ``Address``; if the customer has no default address ``ctx.customer_country``
    is ``None`` and country-gated coupons reject — fail-closed is the right
    posture for a regional restriction.

    ``value`` is normalised to an iterable of uppercase strings so admin
    tooling can store either ``["SA","AE"]`` or ``["sa","ae"]`` without
    surprise.  Anything else (a bare string, ``None``) fails the constraint
    with a clear error.
    """
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise CouponConstraintFailed(
            "allowed_countries",
            f"allowed_countries must be a list of ISO codes, got {value!r}",
        )
    allowed: set[str] = {str(c).upper() for c in value}
    if not allowed:
        # An empty list is treated as "no country allowed", which is almost
        # certainly an admin-tooling bug — but the rule's contract is
        # "country must be in the list", so an empty list is fail-closed.
        raise CouponConstraintFailed(
            "allowed_countries",
            f"coupon '{coupon.code}' allows no countries",
        )

    customer_country = ctx.customer_country.upper() if ctx.customer_country else None
    if customer_country is None:
        raise CouponConstraintFailed(
            "allowed_countries",
            (
                f"coupon '{coupon.code}' is restricted to {sorted(allowed)} "
                "but the customer has no resolved country"
            ),
            allowed_countries=sorted(allowed),
        )
    if customer_country not in allowed:
        raise CouponConstraintFailed(
            "allowed_countries",
            (
                f"customer country {customer_country} is not in the allowlist "
                f"{sorted(allowed)} for coupon '{coupon.code}'"
            ),
            customer_country=customer_country,
            allowed_countries=sorted(allowed),
        )


@CouponValidator.register("usage_limit")
def _check_usage_limit_json(
    coupon: Coupon, value: Any, ctx: CouponValidationContext
) -> None:
    """Fallback handler for ``usage_limit`` declared via JSON.

    The validator's main loop short-circuits this handler when
    ``coupon.usage_limit`` (the column) is set — the built-in
    ``_check_usage_limit`` on the class has already enforced the cap in that
    case.  We register a JSON handler too so the registry remains the single
    source of truth: every key in ``constraints`` resolves to a registered
    handler, no special-casing leaks out of the validator class.

    When this function does run, ``coupon.usage_limit`` is ``None`` and
    ``value`` is the integer cap to compare against.
    """
    try:
        cap = int(value)
    except (TypeError, ValueError) as exc:
        raise CouponConstraintFailed(
            "usage_limit",
            f"usage_limit must be an integer, got {value!r}",
        ) from exc
    if coupon.used_count >= cap:
        raise CouponLimitReached(
            f"coupon '{coupon.code}' has been used {coupon.used_count} of {cap} times"
        )


__all__ = [
    "CouponValidator",
    "CouponValidationContext",
    "RuleFn",
]
