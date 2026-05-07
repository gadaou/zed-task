"""Coupon services — business logic for the coupon aggregate.

Implements PROJECT_SPEC §2 ops 3–4 (apply/remove coupon) following the §4.1
service-layer pattern: thin views, all the rules and transactions live here.

Public API
----------
``CouponService`` is exposed as a class (not module-level functions) per the
project requirements for this iteration.  Its public methods are:

* ``apply_coupon_to_cart(cart, coupon_code)``
* ``remove_coupon_from_cart(cart, coupon_id)``
* ``revalidate_cart_coupons(cart)`` — the hook checkout will call to
  re-evaluate every applied coupon against the cart's *current* state
  (PROJECT_SPEC §5.3: "state can drift between apply and checkout").

All three methods are wrapped in ``transaction.atomic`` so a partial failure
(e.g. validator passes but the unique constraint on ``CartCoupon`` fires)
never leaves the cart with a half-updated discount or the coupon's
``used_count`` incremented without a corresponding row.

Concurrency model (PROJECT_SPEC §3.4)
-------------------------------------
1. The coupon row is fetched with ``select_for_update`` so two concurrent
   apply attempts on the same coupon serialize at the DB layer — the second
   attempt sees the post-increment ``used_count`` and re-validates.
2. The cart row is also fetched with ``select_for_update`` so the discount
   recalculation cannot race with an in-flight ``add_product_to_cart`` /
   ``remove_product_from_cart`` (those calls also lock the cart row).
3. ``used_count`` is incremented via a *conditional* UPDATE
   (``WHERE used_count < usage_limit``).  Even if a concurrent transaction
   somehow slips past the row lock — e.g. on a backend that downgrades
   ``select_for_update`` (SQLite) — the conditional update returns 0 rows
   when the cap is reached and the service translates that to
   ``CouponLimitReached``.  This is the belt-and-suspenders the project
   requirement calls for: validator + conditional increment + DB CHECK
   ``ck_coupon_used_within_limit`` together close every window.
4. Redis distributed locking (``lock:coupon:{tenant}:{code}``, §3.4) is
   deferred to the checkout iteration; row locks + the conditional update
   are sufficient for apply/remove correctness, since neither path performs
   an external call.

Stacking policy
---------------
``CouponService.STACKING_POLICY`` (a class attribute) defines how multiple
coupons combine on a single cart.  The default is
``StackingPolicy.ONE_PER_DISCOUNT_TYPE``: a cart may carry at most one
PERCENTAGE coupon and at most one FIXED coupon.  Real-world retail systems
are full of subtle stacking rules; we pick a sane, easy-to-explain default
and expose the policy as a class attribute so a future iteration can ship
``UNLIMITED`` (no stacking guard) or ``SINGLE_ONLY`` (any second coupon is
rejected) without changing the apply path.

Discount stacking *math* — when multiple coupons are allowed — is also
deterministic: PERCENTAGE coupons are computed against the original
subtotal, FIXED coupons are computed against ``subtotal - already_discounted``
so the customer is never charged below zero.  The ordering matches the
``_compute_discount`` implementation; tests pin it.

ADR-NOTE: ``CouponService`` carries no instance state today other than the
injected validator.  It is exposed as a class because the project prompt
specifies a "Service class".  When a future iteration adds dependencies (a
Redis client, a metrics emitter) they will be injected via ``__init__`` so
the dependency graph is explicit.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Iterable
from uuid import UUID

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.cart.models import Cart
from apps.core.cache import schedule_cart_cache_invalidation
from apps.coupon.exceptions import (
    CouponAlreadyApplied,
    CouponDomainError,
    CouponLimitReached,
    CouponNotFound,
    CouponStackingViolation,
)
from apps.coupon.models import CartCoupon, Coupon
from apps.coupon.validators import CouponValidationContext, CouponValidator


# Two decimal places matches every currency we care about and the
# ``decimal_places=2`` on the schema.  ROUND_HALF_UP is the conventional
# retail rounding mode (banker's rounding would surprise customers).
_MONEY_QUANTUM = Decimal("0.01")


class StackingPolicy(str, Enum):
    """How coupons combine on a single cart.

    ``str``-based Enum so the value can be logged / serialized as plain text
    without an extra ``.value`` lookup at every call site.
    """

    #: Any number of distinct coupons may be applied; the only guard is the
    #: per-pair uniqueness on (cart, coupon).
    UNLIMITED = "unlimited"

    #: Default. At most one coupon of each ``Coupon.DiscountType`` per cart
    #: — i.e. one PERCENTAGE *and* one FIXED may stack, but two PERCENTAGE
    #: coupons cannot.  Matches the most common retail behaviour: customers
    #: see "extra X% off" stack with a "$Y off" voucher.
    ONE_PER_DISCOUNT_TYPE = "one_per_discount_type"

    #: At most one coupon, of any type, per cart.
    SINGLE_ONLY = "single_only"


class CouponService:
    """Service object for applying, removing, and revalidating coupons.

    See module docstring for the concurrency model and stacking policy.
    Methods are intentionally short and delegate constraint evaluation to
    ``CouponValidator`` — keeping the service free of business rules makes
    the next iteration's rule additions a one-line registry change.
    """

    #: Class-level stacking policy.  Subclassing or monkeypatching at
    #: settings-load time is the supported path for changing it; tests set
    #: this on a per-instance basis via ``__init__``.
    STACKING_POLICY: StackingPolicy = StackingPolicy.ONE_PER_DISCOUNT_TYPE

    def __init__(
        self,
        validator: CouponValidator | None = None,
        stacking_policy: StackingPolicy | None = None,
    ) -> None:
        # Validator is injected so tests can swap in a stricter / mock variant
        # without touching service code.
        self._validator = validator or CouponValidator()
        # Per-instance override for the stacking policy — falls back to the
        # class-level default when the caller does not specify.
        self._stacking_policy = stacking_policy or self.STACKING_POLICY

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    @transaction.atomic
    def apply_coupon_to_cart(self, cart: Cart, coupon_code: str) -> Cart:
        """Apply ``coupon_code`` to ``cart`` and update the cart's discount totals.

        Steps (all inside the same ``transaction.atomic``):

        1. Resolve the coupon by ``(tenant_active_from_context, code)``,
           locking the row with ``select_for_update``.  A code that belongs
           to another tenant raises ``CouponNotFound`` — the §3.2
           tenant-isolation invariant is preserved.
        2. Lock the cart row with ``select_for_update``.
        3. Idempotency pre-check: if the coupon is already applied to this
           cart raise ``CouponAlreadyApplied``.
        4. Stacking guard: enforce the active ``StackingPolicy``.  Raises
           ``CouponStackingViolation`` on a conflict.
        5. Build a ``CouponValidationContext`` from the cart and the
           customer's default address.
        6. Run ``CouponValidator.validate`` — raises a typed domain error
           on any failure.
        7. Compute the discount snapshot and insert ``CartCoupon``.
        8. **Conditional** atomic increment of ``Coupon.used_count``.  The
           UPDATE includes ``WHERE used_count < usage_limit`` (when a cap is
           set) so even if the validator's check raced ahead of an external
           usage commit, the increment fails closed and we surface
           ``CouponLimitReached`` instead of overshooting.
        9. Recalculate the cart's denormalised discount totals.

        Args:
            cart: The Cart aggregate to mutate.  Must already be persisted.
            coupon_code: The natural identifier of the coupon (case-sensitive,
                matching ``Coupon.code``).

        Returns:
            The same Cart instance refreshed from the database with
            ``discount_amount``, ``total_after_discount``, and ``version``
            updated.

        Raises:
            CouponNotFound: No active coupon with this code in this tenant.
            CouponInactive / CouponExpired / CouponLimitReached /
            CouponConstraintFailed: validator rejected the apply.
            CouponAlreadyApplied: this coupon is already on this cart.
            CouponStackingViolation: the active stacking policy forbids
                combining this coupon with what is already on the cart.
        """
        from apps.cart.services import recalculate_cart

        try:
            # Tenant scoping is automatic via TenantAwareManager — a code that
            # belongs to another tenant simply doesn't appear in the queryset.
            # ``is_active=False`` coupons are excluded so a deactivated coupon
            # surfaces as "not found" rather than "inactive" — both lead to
            # the same client UX, but "not found" leaks less.
            coupon = Coupon.objects.select_for_update().get(
                code=coupon_code,
                is_active=True,
            )
        except Coupon.DoesNotExist as exc:
            raise CouponNotFound(
                f"no active coupon with code '{coupon_code}' in this tenant"
            ) from exc

        # Lock the cart row.  Order: coupon → cart, fixed across apply and
        # remove to avoid the classic A→B / B→A deadlock pattern.
        locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)

        # Idempotency guard — re-apply of the same coupon to the same cart.
        # Held under the cart row lock so a concurrent apply on the same
        # cart serializes behind us and cannot race past this check.
        if CartCoupon.objects.filter(cart=locked_cart, coupon=coupon).exists():
            raise CouponAlreadyApplied(
                f"coupon '{coupon.code}' is already applied to cart {cart.pk}"
            )

        self._enforce_stacking_policy(locked_cart, coupon)

        ctx = self._build_context(locked_cart)
        self._validator.validate(coupon, ctx)

        discount_amount = self._compute_discount(coupon, locked_cart)

        CartCoupon.objects.create(
            cart=locked_cart,
            coupon=coupon,
            discount_amount=discount_amount,
            currency=locked_cart.currency,
        )

        # Belt-and-suspenders increment.  See ``_atomic_increment_used_count``
        # docstring for the race-safety argument; an overshoot here triggers
        # transaction rollback, the ``CartCoupon`` row is undone, and the
        # caller sees ``CouponLimitReached``.
        self._atomic_increment_used_count(coupon)

        result = recalculate_cart(locked_cart)
        schedule_cart_cache_invalidation(locked_cart.tenant_id, locked_cart.user_id)
        return result

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    @transaction.atomic
    def remove_coupon_from_cart(self, cart: Cart, coupon_id: UUID) -> Cart:
        """Remove ``coupon_id`` from ``cart``; idempotent if not applied.

        ``coupon_id`` is the Coupon UUID (matching the URL shape
        ``DELETE /v1/carts/{cart_id}/coupons/{id}``), not the ``CartCoupon``
        row's UUID.  This keeps the public API symmetric with apply, where
        the client identifies the coupon by code or id rather than by the
        join-row's id.

        Steps:

        1. Lock the cart row.
        2. Locate the matching ``CartCoupon`` row, if any, with a row lock.
        3. If found: delete it and decrement ``Coupon.used_count`` (clamped
           at zero so a buggy double-remove cannot underflow the counter —
           the DB column is ``PositiveIntegerField`` and would refuse).
        4. Recalculate the cart's discount totals.

        Idempotency: removing a coupon that was never applied returns the
        cart unchanged (still triggers a recalculate to bump ``version`` —
        consistent with ``cart.services.remove_product_from_cart``).
        """
        from apps.cart.services import recalculate_cart

        locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)

        cart_coupon = (
            CartCoupon.objects.select_for_update()
            .filter(cart=locked_cart, coupon_id=coupon_id)
            .first()
        )

        if cart_coupon is not None:
            # Conditional decrement — clamp at zero.  ``WHERE used_count > 0``
            # mirrors the ``WHERE used_count < usage_limit`` guard on apply
            # and ensures we never underflow even if some path forgot to
            # increment in the first place.
            Coupon.objects.select_for_update().filter(
                pk=coupon_id, used_count__gt=0
            ).update(used_count=F("used_count") - 1)
            cart_coupon.delete()

        result = recalculate_cart(locked_cart)
        schedule_cart_cache_invalidation(locked_cart.tenant_id, locked_cart.user_id)
        return result

    # ------------------------------------------------------------------
    # Revalidate (the hook CheckoutService will call)
    # ------------------------------------------------------------------

    @transaction.atomic
    def revalidate_cart_coupons(self, cart: Cart) -> Cart:
        """Re-evaluate every coupon currently applied to ``cart``.

        Implements PROJECT_SPEC §5.3 ("state can drift between apply and
        checkout") as a service-layer hook.  CheckoutService — which lands
        in a later iteration — will call this inside its checkout
        ``transaction.atomic`` *after* it locks the cart, *before* it
        finalises the order.  The contract:

        * If every applied coupon still passes the validator, the function
          returns the (recalculated) cart unchanged.
        * Otherwise the **first** failing coupon raises its typed domain
          error (``CouponExpired``, ``CouponInactive``, ``CouponLimitReached``,
          or ``CouponConstraintFailed``) — checkout aborts and the client
          sees the same error taxonomy used at apply-time.

        Why "first failure raises" rather than "collect all failures":
        checkout is the linearization point (PROJECT_SPEC §5.3), and the
        client's UX is "fix this and retry".  A single typed error gives the
        client a clear next step; we can switch to a multi-error variant if
        the checkout UX ever needs to surface the full list.

        Concurrency: opens its own ``transaction.atomic`` and locks the cart
        row + every coupon row referenced by its ``CartCoupon``s.  Safe to
        call standalone in tests; CheckoutService will instead nest this
        inside its own atomic block (``transaction.atomic`` is reentrant via
        savepoints).

        Args:
            cart: The Cart aggregate whose applied coupons must be checked.

        Returns:
            The same Cart instance, recalculated.

        Raises:
            CouponDomainError: any subclass — first failing rule wins.
        """
        from apps.cart.services import recalculate_cart

        locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)

        # Lock every applied coupon row up-front so the validator and the
        # subsequent checkout step (when it lands) see a stable snapshot.
        # ``select_related("coupon")`` avoids an N+1 against the coupon table.
        applications = (
            CartCoupon.objects.select_for_update()
            .filter(cart=locked_cart)
            .select_related("coupon")
        )

        ctx = self._build_context(locked_cart)
        for application in applications:
            self._validator.validate(application.coupon, ctx)

        return recalculate_cart(locked_cart)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _enforce_stacking_policy(self, cart: Cart, candidate: Coupon) -> None:
        """Reject ``candidate`` if it cannot stack with what is already on ``cart``.

        Implements ``StackingPolicy``:

        * ``UNLIMITED`` — never raises.
        * ``SINGLE_ONLY`` — any existing application is a violation.
        * ``ONE_PER_DISCOUNT_TYPE`` — the cart may carry at most one coupon
          of each ``Coupon.DiscountType``; a second of the same type is a
          violation.

        Caller must hold the cart row lock so the existing-applications
        query is consistent with the subsequent insert.
        """
        if self._stacking_policy is StackingPolicy.UNLIMITED:
            return

        existing: Iterable[CartCoupon] = CartCoupon.objects.filter(
            cart=cart
        ).select_related("coupon")

        if self._stacking_policy is StackingPolicy.SINGLE_ONLY:
            conflict = next(iter(existing), None)
            if conflict is not None:
                raise CouponStackingViolation(
                    (
                        "stacking policy 'single_only' rejects a second coupon — "
                        f"cart already has coupon '{conflict.coupon.code}'"
                    ),
                    policy=self._stacking_policy.value,
                    existing_coupon_id=str(conflict.coupon_id),
                )
            return

        # ONE_PER_DISCOUNT_TYPE — most-common production choice.
        for application in existing:
            if application.coupon.discount_type == candidate.discount_type:
                raise CouponStackingViolation(
                    (
                        f"stacking policy 'one_per_discount_type' rejects a "
                        f"second {candidate.discount_type} coupon — cart "
                        f"already has coupon '{application.coupon.code}'"
                    ),
                    policy=self._stacking_policy.value,
                    existing_coupon_id=str(application.coupon_id),
                    discount_type=candidate.discount_type,
                )

    def _atomic_increment_used_count(self, coupon: Coupon) -> None:
        """Increment ``coupon.used_count`` race-safely.

        Two cases:

        * ``coupon.usage_limit is None`` — no cap, a plain F-expression
          increment is sufficient.  No race is possible because there is no
          cap to overshoot.
        * ``coupon.usage_limit`` is set — issue a *conditional* UPDATE with
          ``WHERE used_count < usage_limit``.  This is the canonical
          race-safety pattern: even if two transactions both saw
          ``used_count = limit - 1`` and both raced past the validator, the
          second UPDATE matches zero rows and we raise
          ``CouponLimitReached``.  The transaction (which already created
          the corresponding ``CartCoupon``) is rolled back by the
          ``@transaction.atomic`` boundary on ``apply_coupon_to_cart``,
          leaving the system consistent.

        This is the implementation that satisfies:

        * PROJECT_SPEC §3.4 "the database row lock + version column is the
          actual safety net"
        * The project requirement "after select_for_update on Coupon: re-check
          usage_limit inside transaction; prevent over-increment".

        ``select_for_update`` already serializes most contention; the
        conditional UPDATE is the additional guard for backends that
        downgrade row locks (SQLite — ``select_for_update`` is a no-op) and
        for any future code path that fetches the coupon without a lock.
        """
        if coupon.usage_limit is None:
            Coupon.objects.filter(pk=coupon.pk).update(used_count=F("used_count") + 1)
            return

        rows = Coupon.objects.filter(
            pk=coupon.pk,
            used_count__lt=F("usage_limit"),
        ).update(used_count=F("used_count") + 1)
        if rows == 0:
            # Conditional update matched no rows — either the cap was
            # already reached when we got here, or another transaction
            # incremented us up to the cap between the validator and the
            # update.  Either way: refuse the apply.
            raise CouponLimitReached(
                (
                    f"coupon '{coupon.code}' has reached its usage cap "
                    f"({coupon.usage_limit}) — racing increment refused"
                )
            )

    @staticmethod
    def _build_context(cart: Cart) -> CouponValidationContext:
        """Build the ``CouponValidationContext`` consumed by every rule.

        The customer's country is resolved from the default ``Address``;
        when the customer has no default address the country is ``None`` and
        country-gated rules will reject (fail-closed).
        """
        from apps.addresses.models import Address

        country = (
            Address.objects.filter(
                user_id=cart.user_id,
                is_default=True,
                deleted_at__isnull=True,
            )
            .values_list("country", flat=True)
            .first()
        )
        return CouponValidationContext(
            cart_total=cart.total_price,
            cart_currency=cart.currency,
            customer_country=country,
            now=timezone.now(),
        )

    @staticmethod
    def _compute_discount(coupon: Coupon, cart: Cart) -> Decimal:
        """Compute the per-coupon discount snapshot at apply-time.

        * PERCENTAGE — ``cart.total_price * value / 100``, quantised to two
          decimals (ROUND_HALF_UP, the conventional retail rounding).
        * FIXED — ``min(value, cart.total_price - already_discounted)``.
          Clamped to a non-negative value so a fixed coupon larger than the
          subtotal does not drive ``total_after_discount`` below zero (the
          DB CHECK ``ck_cart_total_after_discount_nonneg`` is the safety net).

        Pinning these formulas in code (and in tests) is what makes the
        stacking math deterministic — see the module docstring's "Stacking
        policy" section.
        """
        if coupon.discount_type == Coupon.DiscountType.PERCENTAGE:
            raw = (cart.total_price * coupon.value) / Decimal("100")
            return raw.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)

        # FIXED branch.
        already = sum(
            (cc.discount_amount for cc in cart.applied_coupons.all()),
            start=Decimal("0.00"),
        )
        remaining = cart.total_price - already
        if remaining <= 0:
            return Decimal("0.00")
        return min(coupon.value, remaining).quantize(
            _MONEY_QUANTUM, rounding=ROUND_HALF_UP
        )


__all__ = ["CouponDomainError", "CouponService", "StackingPolicy"]
