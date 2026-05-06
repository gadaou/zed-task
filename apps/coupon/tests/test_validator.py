"""Unit tests for ``apps.coupon.validators.CouponValidator``.

Coverage matrix (PROJECT_SPEC §6.5 — services/coupons.py at 95% target):

* Built-in checks
    - ``is_active`` False → CouponInactive
    - ``starts_at`` in the future → CouponExpired
    - ``ends_at`` in the past → CouponExpired
    - column ``usage_limit`` reached → CouponLimitReached
    - JSON ``usage_limit`` reached when column is null → CouponLimitReached
* Rule: ``min_total``
    - subtotal meets threshold → passes
    - subtotal below threshold → CouponConstraintFailed("min_total")
    - non-numeric value → CouponConstraintFailed("min_total")
* Rule: ``allowed_countries``
    - matching country → passes
    - non-matching country → CouponConstraintFailed("allowed_countries")
    - missing customer country → CouponConstraintFailed("allowed_countries")
    - case-insensitive matching
    - empty list rejects (fail-closed)
    - non-iterable value → CouponConstraintFailed
* Registry mechanism
    - unknown JSON key → CouponConstraintFailed("<key>", "unknown ...")
    - dynamically registered rule is picked up by validate()

These tests construct ``Coupon`` instances in memory (no ``.save()``) — the
validator never touches the DB, so unit isolation is achievable here and
keeps the suite fast.  ``django_db`` fixtures are not requested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any

import pytest

from apps.coupon.exceptions import (
    CouponConstraintFailed,
    CouponExpired,
    CouponInactive,
    CouponLimitReached,
)
from apps.coupon.models import Coupon
from apps.coupon.validators import CouponValidationContext, CouponValidator


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def make_coupon(
    *,
    code: str = "TEST",
    discount_type: str = Coupon.DiscountType.PERCENTAGE,
    value: Decimal | str | int = Decimal("10"),
    currency: str | None = None,
    constraints: dict[str, Any] | None = None,
    usage_limit: int | None = None,
    used_count: int = 0,
    is_active: bool = True,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> Coupon:
    """Build an unsaved ``Coupon`` for validator-only tests.

    The validator inspects attributes only; persistence is unnecessary for
    correctness checks.  Bypassing ``save()`` keeps these tests independent
    of the DB layer and ~50x faster than the integration suite.
    """
    if discount_type == Coupon.DiscountType.FIXED and currency is None:
        currency = "USD"
    return Coupon(
        code=code,
        discount_type=discount_type,
        value=Decimal(str(value)),
        currency=currency,
        constraints=constraints or {},
        usage_limit=usage_limit,
        used_count=used_count,
        is_active=is_active,
        starts_at=starts_at,
        ends_at=ends_at,
    )


def ctx(
    *,
    cart_total: Decimal | str | int = Decimal("100"),
    cart_currency: str = "USD",
    customer_country: str | None = "US",
    now: datetime | None = None,
) -> CouponValidationContext:
    """Build a ``CouponValidationContext`` with sensible defaults."""
    return CouponValidationContext(
        cart_total=Decimal(str(cart_total)),
        cart_currency=cart_currency,
        customer_country=customer_country,
        now=now or datetime(2026, 5, 6, 12, 0, 0, tzinfo=dt_timezone.utc),
    )


# ----------------------------------------------------------------------
# Built-in: is_active
# ----------------------------------------------------------------------


class TestIsActiveCheck:
    def test_active_coupon_passes(self) -> None:
        CouponValidator().validate(make_coupon(is_active=True), ctx())

    def test_inactive_coupon_raises(self) -> None:
        with pytest.raises(CouponInactive):
            CouponValidator().validate(make_coupon(is_active=False), ctx())


# ----------------------------------------------------------------------
# Built-in: validity window
# ----------------------------------------------------------------------


class TestValidityWindowCheck:
    """``starts_at`` and ``ends_at`` are independently optional."""

    def test_window_unbounded_passes(self) -> None:
        CouponValidator().validate(make_coupon(), ctx())

    def test_inside_window_passes(self) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=dt_timezone.utc)
        coupon = make_coupon(
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=1),
        )
        CouponValidator().validate(coupon, ctx(now=now))

    def test_not_yet_valid_raises(self) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=dt_timezone.utc)
        coupon = make_coupon(starts_at=now + timedelta(minutes=1))
        with pytest.raises(CouponExpired) as exc_info:
            CouponValidator().validate(coupon, ctx(now=now))
        assert "not yet valid" in str(exc_info.value)

    def test_past_end_raises(self) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=dt_timezone.utc)
        coupon = make_coupon(ends_at=now - timedelta(minutes=1))
        with pytest.raises(CouponExpired) as exc_info:
            CouponValidator().validate(coupon, ctx(now=now))
        assert "expired" in str(exc_info.value)

    def test_exact_boundary_inclusive(self) -> None:
        """A coupon valid at exactly its start is usable, and at exactly its end too.

        Documents the inclusive-bound semantics: ``starts_at <= now <= ends_at``.
        Important so admin tooling can set "midnight to midnight" windows
        without an off-by-one second.
        """
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=dt_timezone.utc)
        coupon_at_start = make_coupon(starts_at=now)
        CouponValidator().validate(coupon_at_start, ctx(now=now))

        coupon_at_end = make_coupon(ends_at=now)
        CouponValidator().validate(coupon_at_end, ctx(now=now))


# ----------------------------------------------------------------------
# Built-in: usage_limit (column-first, JSON fallback)
# ----------------------------------------------------------------------


class TestUsageLimitCheck:
    def test_under_column_limit_passes(self) -> None:
        coupon = make_coupon(usage_limit=10, used_count=3)
        CouponValidator().validate(coupon, ctx())

    def test_at_column_limit_raises(self) -> None:
        coupon = make_coupon(usage_limit=5, used_count=5)
        with pytest.raises(CouponLimitReached):
            CouponValidator().validate(coupon, ctx())

    def test_no_cap_passes(self) -> None:
        """``usage_limit=None`` and no JSON key means the rule is a no-op."""
        CouponValidator().validate(
            make_coupon(usage_limit=None, used_count=9999), ctx()
        )

    def test_json_fallback_when_column_is_null(self) -> None:
        coupon = make_coupon(
            usage_limit=None,
            used_count=2,
            constraints={"usage_limit": 2},
        )
        with pytest.raises(CouponLimitReached):
            CouponValidator().validate(coupon, ctx())

    def test_column_wins_over_json(self) -> None:
        """When both column and JSON are set, the column is authoritative.

        Proves the locked design decision ("column-first"): a JSON ``usage_limit``
        is ignored entirely if the column is set, even if the JSON value is
        smaller (which would otherwise make the JSON tighter).
        """
        # Column allows up to 100; JSON would say 1.  The column wins, so
        # used_count=5 is still under the cap.
        coupon = make_coupon(
            usage_limit=100,
            used_count=5,
            constraints={"usage_limit": 1},
        )
        CouponValidator().validate(coupon, ctx())

    def test_json_non_integer_raises_constraint_failed(self) -> None:
        coupon = make_coupon(
            usage_limit=None,
            constraints={"usage_limit": "not-a-number"},
        )
        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponValidator().validate(coupon, ctx())
        assert exc_info.value.constraint_key == "usage_limit"


# ----------------------------------------------------------------------
# Rule: min_total
# ----------------------------------------------------------------------


class TestMinTotalRule:
    def test_subtotal_meets_threshold(self) -> None:
        coupon = make_coupon(constraints={"min_total": "100.00"})
        CouponValidator().validate(coupon, ctx(cart_total="100.00"))

    def test_subtotal_above_threshold(self) -> None:
        coupon = make_coupon(constraints={"min_total": "50"})
        CouponValidator().validate(coupon, ctx(cart_total="100.00"))

    def test_subtotal_below_threshold_raises(self) -> None:
        coupon = make_coupon(constraints={"min_total": "100.00"})
        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponValidator().validate(coupon, ctx(cart_total="99.99"))
        assert exc_info.value.constraint_key == "min_total"

    def test_value_can_be_int(self) -> None:
        coupon = make_coupon(constraints={"min_total": 50})
        CouponValidator().validate(coupon, ctx(cart_total="50"))

    def test_value_can_be_string(self) -> None:
        coupon = make_coupon(constraints={"min_total": "50.00"})
        CouponValidator().validate(coupon, ctx(cart_total="50.00"))

    def test_non_numeric_value_raises(self) -> None:
        coupon = make_coupon(constraints={"min_total": "not-a-decimal"})
        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponValidator().validate(coupon, ctx())
        assert exc_info.value.constraint_key == "min_total"


# ----------------------------------------------------------------------
# Rule: allowed_countries
# ----------------------------------------------------------------------


class TestAllowedCountriesRule:
    def test_matching_country_passes(self) -> None:
        coupon = make_coupon(constraints={"allowed_countries": ["SA", "AE"]})
        CouponValidator().validate(coupon, ctx(customer_country="SA"))

    def test_non_matching_country_raises(self) -> None:
        coupon = make_coupon(constraints={"allowed_countries": ["SA", "AE"]})
        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponValidator().validate(coupon, ctx(customer_country="US"))
        assert exc_info.value.constraint_key == "allowed_countries"

    def test_missing_customer_country_raises(self) -> None:
        """No default address → country-gated coupon rejects (fail-closed)."""
        coupon = make_coupon(constraints={"allowed_countries": ["SA"]})
        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponValidator().validate(coupon, ctx(customer_country=None))
        assert exc_info.value.constraint_key == "allowed_countries"
        # Detail surfaces the issue without exposing the customer's identity.
        assert "no resolved country" in str(exc_info.value)

    def test_case_insensitive_matching(self) -> None:
        """Both list entries and customer country are uppercased before comparing.

        Lets admin tooling store ISO codes in either case; the validator's
        contract is "ISO 3166-1 alpha-2", which is conventionally uppercase.
        """
        coupon = make_coupon(constraints={"allowed_countries": ["sa", "AE"]})
        CouponValidator().validate(coupon, ctx(customer_country="sa"))
        CouponValidator().validate(coupon, ctx(customer_country="Sa"))

    def test_empty_list_fails_closed(self) -> None:
        """An empty allowlist allows nobody — the safe default for a typo."""
        coupon = make_coupon(constraints={"allowed_countries": []})
        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponValidator().validate(coupon, ctx(customer_country="SA"))
        assert exc_info.value.constraint_key == "allowed_countries"

    def test_non_iterable_value_raises(self) -> None:
        coupon = make_coupon(constraints={"allowed_countries": "SA"})
        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponValidator().validate(coupon, ctx())
        assert exc_info.value.constraint_key == "allowed_countries"


# ----------------------------------------------------------------------
# Registry mechanism (the "no hardcoded rules" guarantee)
# ----------------------------------------------------------------------


class TestRegistryMechanism:
    def test_unknown_constraint_key_fails_closed(self) -> None:
        """A typo (or a future rule with no registered handler) must NOT be silently allowed.

        This is the core invariant of the JSON-driven validator: the rule set
        is the registry, and anything outside the registry is a hard error.
        """
        coupon = make_coupon(constraints={"min_totsl": "10"})  # typo
        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponValidator().validate(coupon, ctx())
        assert exc_info.value.constraint_key == "min_totsl"
        assert "no validator registered" in str(exc_info.value)

    def test_dynamically_registered_rule_is_picked_up(self) -> None:
        """A new rule registered at runtime is honoured by ``validate()``.

        Proves the registry is the single source of truth — the validator
        does NOT carry an internal list of supported keys.  Test cleans up
        after itself in ``finally`` so it does not leak state into other tests.
        """
        called_with: dict[str, Any] = {}

        @CouponValidator.register("custom_rule")
        def _handler(coupon, value, vctx):  # type: ignore[no-untyped-def]
            called_with["coupon_code"] = coupon.code
            called_with["value"] = value
            called_with["country"] = vctx.customer_country

        try:
            coupon = make_coupon(constraints={"custom_rule": 42})
            CouponValidator().validate(coupon, ctx(customer_country="EG"))
            assert called_with == {
                "coupon_code": "TEST",
                "value": 42,
                "country": "EG",
            }
        finally:
            # Avoid leaking the test rule into the global registry.
            CouponValidator._rules.pop("custom_rule", None)

    def test_dynamic_rule_can_reject(self) -> None:
        @CouponValidator.register("always_fail")
        def _handler(coupon, value, vctx):  # type: ignore[no-untyped-def]
            raise CouponConstraintFailed("always_fail", "by design")

        try:
            coupon = make_coupon(constraints={"always_fail": True})
            with pytest.raises(CouponConstraintFailed) as exc_info:
                CouponValidator().validate(coupon, ctx())
            assert exc_info.value.constraint_key == "always_fail"
        finally:
            CouponValidator._rules.pop("always_fail", None)


# ----------------------------------------------------------------------
# Order of evaluation
# ----------------------------------------------------------------------


class TestEvaluationOrder:
    """Built-in checks must run before JSON rules.

    Rationale: the most common reasons a coupon is unusable (deactivated,
    expired, capped) should surface their typed exception even when the JSON
    constraints would *also* fail — the typed exception carries more
    actionable information for the client.
    """

    def test_inactive_takes_priority_over_min_total(self) -> None:
        coupon = make_coupon(
            is_active=False,
            constraints={"min_total": "9999"},  # would also fail
        )
        with pytest.raises(CouponInactive):
            CouponValidator().validate(coupon, ctx(cart_total="0"))

    def test_expired_takes_priority_over_allowed_countries(self) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=dt_timezone.utc)
        coupon = make_coupon(
            ends_at=now - timedelta(seconds=1),
            constraints={"allowed_countries": ["SA"]},
        )
        with pytest.raises(CouponExpired):
            CouponValidator().validate(
                coupon, ctx(now=now, customer_country="US")
            )

    def test_usage_limit_takes_priority_over_json_rules(self) -> None:
        coupon = make_coupon(
            usage_limit=1,
            used_count=1,
            constraints={"min_total": "9999"},
        )
        with pytest.raises(CouponLimitReached):
            CouponValidator().validate(coupon, ctx(cart_total="0"))
