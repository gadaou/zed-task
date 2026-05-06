"""Integration tests for ``apps.coupon.services.CouponService``.

Coverage matrix (PROJECT_SPEC §6.5 — services/coupons.py at 95% target):

* Apply — happy paths
    - Valid PERCENTAGE coupon: row created, totals updated, used_count++
    - Valid FIXED coupon: discount clamped at remaining payable
    - Apply two distinct coupons stacks the discounts
* Apply — invalid coupons
    - Coupon code does not exist → CouponNotFound
    - Coupon is inactive → CouponNotFound (the lookup excludes is_active=False)
    - min_total constraint not met → CouponConstraintFailed, no row,
      used_count unchanged
    - allowed_countries mismatch → CouponConstraintFailed
    - usage_limit at cap → CouponLimitReached
    - Validity window expired → CouponExpired
* Apply — edge cases
    - Re-apply same coupon → CouponAlreadyApplied, used_count unchanged
    - PERCENTAGE on empty cart → discount = 0, row still created
* Remove — happy path
    - Removing an applied coupon deletes the row, decrements used_count,
      recalculates totals
* Remove — edge cases
    - Removing a coupon that is not applied is idempotent (no error,
      used_count unchanged)
    - Removing one of two coupons leaves the other intact
* Tenant isolation
    - Coupon owned by another tenant cannot be applied to this tenant's cart
* Concurrency (race-safe increment)
    - Conditional UPDATE refuses when the cap is reached
    - Simulated race: validator passed but used_count was bumped by another
      committer in between → second commit refused
    - No-cap path is the simple F-update
* Stacking policy
    - ONE_PER_DISCOUNT_TYPE allows PERCENTAGE + FIXED, rejects PCT + PCT
    - SINGLE_ONLY rejects any second coupon
    - UNLIMITED allows arbitrary stacking
* Revalidation hook (CheckoutService integration)
    - All applied coupons still valid → returns recalculated cart
    - Coupon expired since apply → CouponExpired
    - Coupon deactivated since apply → CouponInactive
    - Constraint state drifted (cart shrank below min_total) → CouponConstraintFailed
    - Empty cart with no coupons is a no-op

Every test is marked ``@pytest.mark.django_db(transaction=True)`` because
``select_for_update()`` requires a real transaction (PROJECT_SPEC §3.4).
The default pytest-django savepoint wrapping would silently make the row
locks no-ops on Postgres and outright fail on SQLite.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.cart.services import add_product_to_cart, remove_product_from_cart
from apps.coupon.exceptions import (
    CouponAlreadyApplied,
    CouponConstraintFailed,
    CouponExpired,
    CouponInactive,
    CouponLimitReached,
    CouponNotFound,
    CouponStackingViolation,
)
from apps.coupon.models import CartCoupon, Coupon
from apps.coupon.services import CouponService, StackingPolicy
from apps.tenant.context import tenant_context


pytestmark = pytest.mark.django_db(transaction=True)


# ----------------------------------------------------------------------
# Apply — happy paths
# ----------------------------------------------------------------------


class TestApplyHappyPath:
    def test_percentage_coupon_creates_row_and_updates_totals(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("50.00"))
        add_product_to_cart(cart, product, quantity=2)  # subtotal = 100.00

        coupon = coupon_factory(
            code="PCT10",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),  # 10% off
        )

        service = CouponService()
        updated = service.apply_coupon_to_cart(cart, "PCT10")

        applications = list(CartCoupon.objects.filter(cart=updated))
        assert len(applications) == 1
        assert applications[0].coupon_id == coupon.id
        assert applications[0].discount_amount == Decimal("10.00")
        assert applications[0].currency == "USD"

        assert updated.total_price == Decimal("100.00")
        assert updated.discount_amount == Decimal("10.00")
        assert updated.total_after_discount == Decimal("90.00")

        coupon.refresh_from_db()
        assert coupon.used_count == 1

    def test_fixed_coupon_clamped_at_subtotal(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """A FIXED coupon larger than the subtotal yields a discount of *exactly* the subtotal."""
        cart = cart_factory()
        product = product_factory(price=Decimal("5.00"))
        add_product_to_cart(cart, product, quantity=1)  # subtotal = 5.00

        coupon_factory(
            code="FIX20",
            discount_type=Coupon.DiscountType.FIXED,
            value=Decimal("20.00"),  # bigger than subtotal
            currency="USD",
        )

        updated = CouponService().apply_coupon_to_cart(cart, "FIX20")

        # Discount snapshot is clamped to the subtotal — payable is zero,
        # never negative (DB CHECK ck_cart_total_after_discount_nonneg).
        assert updated.discount_amount == Decimal("5.00")
        assert updated.total_after_discount == Decimal("0.00")

    def test_two_distinct_coupons_stack(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)  # subtotal = 100.00

        coupon_factory(
            code="P10",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),
        )
        coupon_factory(
            code="F5",
            discount_type=Coupon.DiscountType.FIXED,
            value=Decimal("5.00"),
            currency="USD",
        )

        service = CouponService()
        service.apply_coupon_to_cart(cart, "P10")
        updated = service.apply_coupon_to_cart(cart, "F5")

        # 10% off 100 = 10.00; then 5.00 fixed off remaining 90 = 5.00.
        # Total discount = 15.00, payable = 85.00.
        assert updated.discount_amount == Decimal("15.00")
        assert updated.total_after_discount == Decimal("85.00")
        assert CartCoupon.objects.filter(cart=updated).count() == 2

    def test_percentage_on_empty_cart(
        self, cart_factory, coupon_factory
    ) -> None:
        """Edge case: applying to a 0.00 cart yields a 0.00 discount but still records the application."""
        cart = cart_factory()
        coupon_factory(
            code="P50",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("50"),
        )

        updated = CouponService().apply_coupon_to_cart(cart, "P50")

        assert CartCoupon.objects.filter(cart=updated).count() == 1
        assert updated.total_price == Decimal("0.00")
        assert updated.discount_amount == Decimal("0.00")
        assert updated.total_after_discount == Decimal("0.00")


# ----------------------------------------------------------------------
# Apply — invalid coupons
# ----------------------------------------------------------------------


class TestApplyInvalid:
    def test_unknown_coupon_code_raises_not_found(
        self, cart_factory
    ) -> None:
        cart = cart_factory()
        with pytest.raises(CouponNotFound):
            CouponService().apply_coupon_to_cart(cart, "DOES-NOT-EXIST")

    def test_inactive_coupon_surfaces_as_not_found(
        self, cart_factory, coupon_factory
    ) -> None:
        """A deactivated coupon is excluded by the service's lookup filter.

        Documents the deliberate UX choice: clients see "not found" rather than
        "this coupon is disabled" — admin tooling state is not leaked.
        """
        cart = cart_factory()
        coupon_factory(code="DEAD", is_active=False)
        with pytest.raises(CouponNotFound):
            CouponService().apply_coupon_to_cart(cart, "DEAD")

    def test_min_total_not_met_rejects_and_does_not_increment_counter(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """Critical invariant: validator failure leaves zero side-effects."""
        cart = cart_factory()
        product = product_factory(price=Decimal("10.00"))
        add_product_to_cart(cart, product, quantity=1)  # subtotal = 10.00

        coupon = coupon_factory(
            code="MIN50",
            constraints={"min_total": "50"},
        )

        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponService().apply_coupon_to_cart(cart, "MIN50")
        assert exc_info.value.constraint_key == "min_total"

        # No CartCoupon row was inserted.
        assert CartCoupon.objects.filter(cart=cart).count() == 0
        # used_count was NOT incremented (the F-update never ran).
        coupon.refresh_from_db()
        assert coupon.used_count == 0

    def test_allowed_countries_mismatch_rejects(
        self, cart_factory, address_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        address_factory(user_id=cart.user_id, country="US", is_default=True)

        coupon_factory(
            code="SAONLY",
            constraints={"allowed_countries": ["SA", "AE"]},
        )

        with pytest.raises(CouponConstraintFailed) as exc_info:
            CouponService().apply_coupon_to_cart(cart, "SAONLY")
        assert exc_info.value.constraint_key == "allowed_countries"

    def test_allowed_countries_matching_passes(
        self, cart_factory, address_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        address_factory(user_id=cart.user_id, country="SA", is_default=True)

        coupon_factory(
            code="SAONLY",
            constraints={"allowed_countries": ["SA", "AE"]},
        )

        updated = CouponService().apply_coupon_to_cart(cart, "SAONLY")
        assert CartCoupon.objects.filter(cart=updated).count() == 1

    def test_usage_limit_at_cap_raises(
        self, cart_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        coupon_factory(code="ONCE", usage_limit=1, used_count=1)

        with pytest.raises(CouponLimitReached):
            CouponService().apply_coupon_to_cart(cart, "ONCE")

    def test_expired_window_raises(
        self, cart_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        coupon_factory(
            code="OLD",
            ends_at=timezone.now() - timedelta(seconds=10),
        )
        with pytest.raises(CouponExpired):
            CouponService().apply_coupon_to_cart(cart, "OLD")


# ----------------------------------------------------------------------
# Apply — edge: re-apply
# ----------------------------------------------------------------------


class TestApplyReapply:
    def test_reapply_same_coupon_raises_already_applied(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("50.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon = coupon_factory(code="ONCE-PER-CART")

        service = CouponService()
        service.apply_coupon_to_cart(cart, "ONCE-PER-CART")

        with pytest.raises(CouponAlreadyApplied):
            service.apply_coupon_to_cart(cart, "ONCE-PER-CART")

        # The first application is still there; the second was rejected
        # before any side effect.
        assert CartCoupon.objects.filter(cart=cart).count() == 1
        coupon.refresh_from_db()
        # used_count was incremented exactly once.
        assert coupon.used_count == 1


# ----------------------------------------------------------------------
# Remove
# ----------------------------------------------------------------------


class TestRemoveHappyPath:
    def test_remove_applied_coupon_deletes_row_and_decrements_counter(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("50.00"))
        add_product_to_cart(cart, product, quantity=2)  # subtotal = 100.00

        coupon = coupon_factory(
            code="REMOVE-ME",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),
        )

        service = CouponService()
        service.apply_coupon_to_cart(cart, "REMOVE-ME")

        coupon.refresh_from_db()
        assert coupon.used_count == 1

        updated = service.remove_coupon_from_cart(cart, coupon.id)

        assert CartCoupon.objects.filter(cart=updated).count() == 0
        assert updated.discount_amount == Decimal("0.00")
        assert updated.total_after_discount == Decimal("100.00")

        coupon.refresh_from_db()
        assert coupon.used_count == 0


class TestRemoveEdgeCases:
    def test_remove_unapplied_coupon_is_idempotent(
        self, cart_factory, coupon_factory
    ) -> None:
        """Removing a coupon that was never applied succeeds and changes nothing.

        Mirrors the idempotency contract on ``remove_product_from_cart``
        (PROJECT_SPEC §2 row 2 / §6.4 "every endpoint should be safely
        retryable").
        """
        cart = cart_factory()
        coupon = coupon_factory(code="UNUSED", used_count=0)

        # Should not raise.
        updated = CouponService().remove_coupon_from_cart(cart, coupon.id)

        assert CartCoupon.objects.filter(cart=updated).count() == 0
        coupon.refresh_from_db()
        # used_count was never incremented or decremented.
        assert coupon.used_count == 0

    def test_remove_one_of_two_coupons_leaves_other_intact(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        first = coupon_factory(
            code="KEEP",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),
        )
        second = coupon_factory(
            code="DROP",
            discount_type=Coupon.DiscountType.FIXED,
            value=Decimal("5.00"),
            currency="USD",
        )

        service = CouponService()
        service.apply_coupon_to_cart(cart, "KEEP")
        service.apply_coupon_to_cart(cart, "DROP")

        updated = service.remove_coupon_from_cart(cart, second.id)

        remaining = list(CartCoupon.objects.filter(cart=updated))
        assert len(remaining) == 1
        assert remaining[0].coupon_id == first.id
        # Only KEEP's discount is reflected: 10% of 100.00.
        assert updated.discount_amount == Decimal("10.00")
        assert updated.total_after_discount == Decimal("90.00")


# ----------------------------------------------------------------------
# Tenant isolation
# ----------------------------------------------------------------------


class TestTenantIsolation:
    def test_coupon_in_other_tenant_is_not_found(
        self, cart_factory, other_tenant
    ) -> None:
        """A coupon code owned by tenant B cannot be applied to a cart in tenant A.

        Implements the §3.2 invariant: ``TenantAwareManager`` scopes every
        query to the active tenant, so a same-name coupon in another tenant
        is invisible to the lookup — surfaces as ``CouponNotFound``.
        """
        # cart_factory pulled in the ``tenant`` fixture, which activated
        # tenant A's context.
        cart = cart_factory()

        # Switch into tenant B's context to seed a coupon there.
        with tenant_context(other_tenant):
            Coupon.objects.create(
                code="CROSS-TENANT",
                discount_type=Coupon.DiscountType.PERCENTAGE,
                value=Decimal("10"),
                currency=None,
                constraints={},
            )

        # Back in tenant A's context (the ``tenant`` fixture is still
        # active because ``other_tenant`` did not yield inside its with-block
        # — pytest's fixture teardown order restores it).  Looking up the
        # cross-tenant code must surface as not-found.
        with pytest.raises(CouponNotFound):
            CouponService().apply_coupon_to_cart(cart, "CROSS-TENANT")


# ----------------------------------------------------------------------
# Concurrency — usage_limit race safety
# ----------------------------------------------------------------------


class TestUsageLimitConcurrency:
    """Verifies that the conditional ``UPDATE ... WHERE used_count < usage_limit``
    in ``_atomic_increment_used_count`` is the safety net the spec calls for.

    True multi-process concurrency tests need a Postgres test backend
    (PROJECT_SPEC §6.5 calls them out separately as ``concurrency`` tests).
    Here we simulate the race by mutating ``used_count`` directly between
    the validator step and the increment step — that is exactly the state
    a concurrent committer would have produced if its UPDATE landed first.
    """

    def test_increment_at_cap_raises_limit_reached(
        self, cart_factory, coupon_factory
    ) -> None:
        """Direct unit-style test of the conditional increment.

        Pre-conditions the coupon row to ``used_count == usage_limit`` and
        verifies the helper refuses without mutating the row.
        """
        cart = cart_factory()
        # No real cart-coupon flow here — we want to exercise the guard
        # primitive in isolation.
        coupon = coupon_factory(code="CAP", usage_limit=2, used_count=2)

        service = CouponService()
        with pytest.raises(CouponLimitReached):
            service._atomic_increment_used_count(coupon)

        coupon.refresh_from_db()
        # Critical invariant: a refused increment must NOT bump the counter.
        assert coupon.used_count == 2

    def test_increment_no_cap_is_unconditional(
        self, cart_factory, coupon_factory
    ) -> None:
        coupon = coupon_factory(code="UNCAPPED", usage_limit=None, used_count=99)
        CouponService()._atomic_increment_used_count(coupon)

        coupon.refresh_from_db()
        assert coupon.used_count == 100

    def test_simulated_race_validator_passed_then_concurrent_commit_at_cap(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """Simulates: validator at T1 saw ``used_count = limit-1`` (passes),
        concurrent transaction commits at T2 → ``used_count = limit``,
        our UPDATE at T3 must refuse.

        Test technique: a monkeypatched validator bumps ``used_count`` to
        the cap right after the real validator returns.  This stands in
        for the "another transaction committed in the gap" timeline
        without requiring a multi-process Postgres test harness (PROJECT_SPEC
        §6.5 calls out a dedicated ``concurrency`` test layer for that).

        Simulation caveat: the racing UPDATE happens inside the same
        ``transaction.atomic`` as the apply, so when the apply rolls back,
        the simulated bump rolls back with it — leaving ``used_count`` at
        its pre-transaction value (``1``) rather than the cap-reached value
        (``2``) a real concurrent committer would have left behind.  The
        test asserts the rollback semantic (which is the actually-important
        invariant); the racing committer's persisted state is exercised by
        the unit-style ``test_increment_at_cap_raises_limit_reached`` above.

        The behaviours this test pins:
        1. Conditional UPDATE refuses the increment when the cap is reached
           between validator and update → ``CouponLimitReached``.
        2. The apply transaction rolls back atomically: no CartCoupon row,
           no discount, no cart-side mutation.
        """
        cart = cart_factory()
        product = product_factory(price=Decimal("50.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon = coupon_factory(
            code="RACE",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),
            usage_limit=2,
            used_count=1,  # validator will see 1 < 2 → passes
        )

        service = CouponService()
        original_validate = service._validator.validate

        def racing_validate(coupon_arg, ctx):  # type: ignore[no-untyped-def]
            original_validate(coupon_arg, ctx)
            Coupon.objects.filter(pk=coupon_arg.pk).update(used_count=2)

        service._validator.validate = racing_validate  # type: ignore[assignment]

        with pytest.raises(CouponLimitReached):
            service.apply_coupon_to_cart(cart, "RACE")

        # All-or-nothing: the apply transaction rolled back.
        assert CartCoupon.objects.filter(cart=cart).count() == 0
        coupon.refresh_from_db()
        # Pre-transaction value (the racing bump rolls back with the
        # apply — see "Simulation caveat" in the docstring).
        assert coupon.used_count == 1

        cart.refresh_from_db()
        assert cart.discount_amount == Decimal("0.00")
        assert cart.total_after_discount == cart.total_price

    def test_remove_does_not_underflow_used_count(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """Defensive: removing an applied coupon never drives used_count below zero.

        The remove path uses ``WHERE used_count > 0`` for the same
        belt-and-suspenders reason as the apply path.  If a buggy double-
        remove ever fires, the counter clamps at zero rather than
        underflowing the ``PositiveIntegerField``.
        """
        cart = cart_factory()
        product = product_factory(price=Decimal("10.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon = coupon_factory(code="STAY-NONNEG")
        service = CouponService()
        service.apply_coupon_to_cart(cart, "STAY-NONNEG")

        # Manually zero the counter to simulate "remove already happened
        # somewhere else" then call remove again.
        Coupon.objects.filter(pk=coupon.pk).update(used_count=0)

        # remove_coupon_from_cart deletes the CartCoupon and tries to
        # decrement; the conditional clamp prevents going negative.
        service.remove_coupon_from_cart(cart, coupon.id)

        coupon.refresh_from_db()
        assert coupon.used_count == 0


# ----------------------------------------------------------------------
# Stacking policy
# ----------------------------------------------------------------------


class TestStackingPolicy:
    """Stacking is the most-fudgable retail rule — pin it explicitly here.

    Two coupons of the same discount_type can never combine on a single
    cart under the default policy.  Cross-type stacking (PERCENTAGE +
    FIXED) is allowed and produces deterministic math (PCT off subtotal,
    FIXED off remainder).
    """

    # ---- ONE_PER_DISCOUNT_TYPE (default) ----

    def test_default_policy_allows_pct_plus_fixed(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon_factory(code="PCT10", discount_type=Coupon.DiscountType.PERCENTAGE, value=Decimal("10"))
        coupon_factory(
            code="FIX5",
            discount_type=Coupon.DiscountType.FIXED,
            value=Decimal("5.00"),
            currency="USD",
        )

        service = CouponService()
        service.apply_coupon_to_cart(cart, "PCT10")
        updated = service.apply_coupon_to_cart(cart, "FIX5")

        assert CartCoupon.objects.filter(cart=updated).count() == 2
        # 10% off 100 = 10.00; FIXED 5 off the remaining 90 = 5.00.
        assert updated.discount_amount == Decimal("15.00")

    def test_default_policy_rejects_two_percentage_coupons(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon_factory(code="P10", discount_type=Coupon.DiscountType.PERCENTAGE, value=Decimal("10"))
        coupon_factory(code="P20", discount_type=Coupon.DiscountType.PERCENTAGE, value=Decimal("20"))

        service = CouponService()
        service.apply_coupon_to_cart(cart, "P10")

        with pytest.raises(CouponStackingViolation) as exc_info:
            service.apply_coupon_to_cart(cart, "P20")
        assert exc_info.value.extra["policy"] == "one_per_discount_type"

        # Side-effects: only the first coupon is on the cart, used_count
        # only bumped for the first one.
        assert CartCoupon.objects.filter(cart=cart).count() == 1
        p20 = Coupon.objects.get(code="P20")
        assert p20.used_count == 0

    def test_default_policy_rejects_two_fixed_coupons(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon_factory(
            code="F5",
            discount_type=Coupon.DiscountType.FIXED,
            value=Decimal("5.00"),
            currency="USD",
        )
        coupon_factory(
            code="F10",
            discount_type=Coupon.DiscountType.FIXED,
            value=Decimal("10.00"),
            currency="USD",
        )

        service = CouponService()
        service.apply_coupon_to_cart(cart, "F5")
        with pytest.raises(CouponStackingViolation):
            service.apply_coupon_to_cart(cart, "F10")

    # ---- SINGLE_ONLY ----

    def test_single_only_policy_rejects_any_second_coupon(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """Even a cross-type second coupon is rejected when the policy is SINGLE_ONLY."""
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon_factory(code="P10", discount_type=Coupon.DiscountType.PERCENTAGE, value=Decimal("10"))
        coupon_factory(
            code="F5",
            discount_type=Coupon.DiscountType.FIXED,
            value=Decimal("5.00"),
            currency="USD",
        )

        service = CouponService(stacking_policy=StackingPolicy.SINGLE_ONLY)
        service.apply_coupon_to_cart(cart, "P10")

        with pytest.raises(CouponStackingViolation) as exc_info:
            service.apply_coupon_to_cart(cart, "F5")
        assert exc_info.value.extra["policy"] == "single_only"

    # ---- UNLIMITED ----

    def test_unlimited_policy_allows_two_of_same_type(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon_factory(code="P10", discount_type=Coupon.DiscountType.PERCENTAGE, value=Decimal("10"))
        coupon_factory(code="P5", discount_type=Coupon.DiscountType.PERCENTAGE, value=Decimal("5"))

        service = CouponService(stacking_policy=StackingPolicy.UNLIMITED)
        service.apply_coupon_to_cart(cart, "P10")
        updated = service.apply_coupon_to_cart(cart, "P5")

        assert CartCoupon.objects.filter(cart=updated).count() == 2
        # Both PERCENTAGEs stack against the *original* subtotal — pinning
        # the math here is what the module docstring promises.
        # 10% of 100 = 10; 5% of 100 = 5; total = 15.
        assert updated.discount_amount == Decimal("15.00")


# ----------------------------------------------------------------------
# Revalidation — the hook CheckoutService will call
# ----------------------------------------------------------------------


class TestRevalidateCartCoupons:
    """Implements PROJECT_SPEC §5.3 ("state can drift between apply and checkout").

    These tests stand in for the future ``CheckoutService.checkout_cart``
    flow.  When CheckoutService lands it will call
    ``CouponService.revalidate_cart_coupons(cart)`` inside its own atomic
    block; the contract these tests pin must hold.
    """

    def test_no_drift_returns_recalculated_cart(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """Happy path: every coupon still passes, returns cart with refreshed totals."""
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon_factory(
            code="STILL-OK",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),
        )

        service = CouponService()
        service.apply_coupon_to_cart(cart, "STILL-OK")

        revalidated = service.revalidate_cart_coupons(cart)
        assert revalidated.discount_amount == Decimal("10.00")
        assert revalidated.total_after_discount == Decimal("90.00")

    def test_empty_cart_with_no_coupons_is_noop(self, cart_factory) -> None:
        """A cart that never had a coupon revalidates cleanly."""
        cart = cart_factory()
        revalidated = CouponService().revalidate_cart_coupons(cart)
        assert revalidated.discount_amount == Decimal("0.00")

    def test_expired_coupon_during_checkout_raises(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """The headline case: coupon was valid at apply, expired before checkout.

        Reproduces the §5.3 timing window: ends_at was in the future at
        apply-time and has since passed.  The simulated CheckoutService
        call (revalidate_cart_coupons) must surface ``CouponExpired`` so
        the order is not finalised with an invalid discount.
        """
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon = coupon_factory(
            code="EXP-AT-CHECKOUT",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),
            ends_at=timezone.now() + timedelta(hours=1),
        )

        service = CouponService()
        service.apply_coupon_to_cart(cart, "EXP-AT-CHECKOUT")

        # Simulate "checkout starts an hour later" by moving ends_at into
        # the past — equivalent to wall-clock time advancing past it.
        coupon.ends_at = timezone.now() - timedelta(seconds=1)
        coupon.save(update_fields=["ends_at"])

        with pytest.raises(CouponExpired):
            service.revalidate_cart_coupons(cart)

    def test_inactive_coupon_during_checkout_raises(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """An admin deactivated the coupon between apply and checkout."""
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon = coupon_factory(code="WAS-ACTIVE")
        service = CouponService()
        service.apply_coupon_to_cart(cart, "WAS-ACTIVE")

        coupon.is_active = False
        coupon.save(update_fields=["is_active"])

        with pytest.raises(CouponInactive):
            service.revalidate_cart_coupons(cart)

    def test_constraint_drift_during_checkout_raises(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """Cart shrinks below ``min_total`` between apply and checkout.

        Concrete case from PROJECT_SPEC §5.3: the customer added a $50 item
        + applied a coupon that needs subtotal ≥ $50, then removed the
        item.  Apply was valid at the time; checkout must reject.
        """
        cart = cart_factory()
        big_product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, big_product, quantity=1)

        coupon_factory(
            code="MIN50",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),
            constraints={"min_total": "50"},
        )

        service = CouponService()
        service.apply_coupon_to_cart(cart, "MIN50")
        # Customer drops the only item — subtotal is now 0, well under 50.
        remove_product_from_cart(cart, big_product.id)

        with pytest.raises(CouponConstraintFailed) as exc_info:
            service.revalidate_cart_coupons(cart)
        assert exc_info.value.constraint_key == "min_total"

    def test_revalidation_is_read_only_when_all_pass(
        self, cart_factory, product_factory, coupon_factory
    ) -> None:
        """Revalidation must not mutate ``used_count`` or ``CartCoupon`` rows.

        Pinning this invariant matters because checkout will call this
        method on every flow — silently incrementing counters here would
        double-count usage.
        """
        cart = cart_factory()
        product = product_factory(price=Decimal("100.00"))
        add_product_to_cart(cart, product, quantity=1)

        coupon = coupon_factory(
            code="READONLY",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10"),
        )
        service = CouponService()
        service.apply_coupon_to_cart(cart, "READONLY")

        coupon.refresh_from_db()
        assert coupon.used_count == 1

        # Call revalidate several times — none of them should mutate state.
        service.revalidate_cart_coupons(cart)
        service.revalidate_cart_coupons(cart)
        service.revalidate_cart_coupons(cart)

        coupon.refresh_from_db()
        assert coupon.used_count == 1
        assert CartCoupon.objects.filter(cart=cart).count() == 1
