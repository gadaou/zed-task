"""Coupon models — ``Coupon``.

Implements PROJECT_SPEC §2 operations 3–4 (apply/remove coupon) and the bonus
coupon-constraints section (§2 "Coupon constraints").

Design decisions:
- ``discount_type`` is a two-value TextChoices (PERCENTAGE / FIXED).  Adding a
  third type (e.g. BOGO, FREE_SHIPPING) is a field addition + migration; no
  structural change needed.
- ``value`` carries the numeric magnitude of the discount.  For PERCENTAGE it is
  the percentage points (1–100); for FIXED it is an absolute monetary amount
  paired with ``currency``.  A CHECK constraint enforces the ≤ 100 bound for
  PERCENTAGE discounts at the DB layer.
- ``currency`` is nullable: a PERCENTAGE coupon is currency-agnostic.  A FIXED
  coupon requires a currency so the apply-service can reject mismatches.  The
  DB CHECK ck_coupon_currency_iso4217 validates the format when non-null.
- ``constraints`` is an opaque JSONField.  The full constraint schema
  (cart_minimum, country_allowlist, segment, validity_window, product_denylist
  …) is evaluated in the future ``apply_coupon`` service (PROJECT_SPEC §4.3),
  not here.  We do not encode each constraint as a typed column because the set
  is expected to grow without requiring new migrations (PROJECT_SPEC §8 bonus).
- ``used_count`` is a counter incremented by the apply service via
  ``F("used_count") + 1`` inside a ``SELECT FOR UPDATE`` transaction + Redis
  lock on ``lock:coupon:{tenant_id}:{coupon_code}`` (PROJECT_SPEC §3.4).
  A DB CHECK enforces used_count ≤ usage_limit when the limit is set.
- ``starts_at`` / ``ends_at`` are validity-window scaffolding evaluated by the
  service; a DB CHECK guarantees ends_at ≥ starts_at when both are set.

ADR-NOTE §6.2: UUID primary keys use ``uuid.uuid4`` in this iteration.
A ``uuid7()`` helper (time-ordered, B-tree-friendly) will replace this in a
dedicated common/ids.py pass per PROJECT_SPEC §6.2.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models
from django.db.models import F, Q

from apps.tenant.models import TenantAwareModel


class Coupon(TenantAwareModel):
    """A discount coupon scoped to a single tenant.

    A coupon may carry a percentage or fixed-amount discount, optional usage
    caps, an optional validity window, and an opaque constraints blob that the
    ``apply_coupon`` service evaluates at apply-time and again at checkout
    (PROJECT_SPEC §5.3 "state can drift between apply and checkout").

    Lifecycle: coupons are created with ``is_active=True`` and deactivated by
    setting ``is_active=False``; they are never hard-deleted so redemption
    history retains a valid FK target.
    """

    class DiscountType(models.TextChoices):
        PERCENTAGE = "PERCENTAGE", "Percentage"
        FIXED = "FIXED", "Fixed amount"

    # ADR-NOTE §6.2: UUIDv7 (time-ordered) preferred for B-tree performance.
    # uuid4 is the interim; replace with uuid7() when common/ids.py lands.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Natural identifier within a tenant. Max 64 chars covers most real-world
    # coupon code conventions (SUMMER2026, FIRST10OFF, etc.).
    code = models.CharField(max_length=64)

    discount_type = models.CharField(
        max_length=12,
        choices=DiscountType.choices,
    )

    # Numeric magnitude: percentage points for PERCENTAGE, absolute amount for
    # FIXED.  Always > 0 (DB CHECK).  For PERCENTAGE: checked ≤ 100 by DB CHECK.
    value = models.DecimalField(max_digits=12, decimal_places=2)

    # Paired with FIXED discounts; null = PERCENTAGE (currency-agnostic).
    # ISO 4217 three-letter code validated by DB CHECK when non-null.
    currency = models.CharField(max_length=3, null=True, blank=True, default=None)

    # Opaque constraint bag — evaluated by apply_coupon service (PROJECT_SPEC §2
    # bonus).  Expected keys (none required now; all optional):
    #   cart_minimum_amount, cart_minimum_currency,
    #   country_allowlist (list of ISO-3166 alpha-2),
    #   segment (list: "B2B" | "FIRST_PURCHASE" | "VIP" | ...),
    #   product_allowlist (list of UUIDs), product_denylist (list of UUIDs),
    #   category_allowlist (list of UUIDs), category_denylist (list of UUIDs),
    #   per_customer_usage_cap (int)
    constraints = models.JSONField(default=dict)

    # null = unlimited; an integer cap is enforced by DB CHECK used_count ≤ cap.
    usage_limit = models.PositiveIntegerField(null=True, blank=True, default=None)

    # Atomic increment via F-expression + SELECT FOR UPDATE (PROJECT_SPEC §3.4).
    used_count = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True)

    # Validity window — both nullable.  DB CHECK enforces ends_at ≥ starts_at.
    # Evaluated by the apply-service against timezone.now() at apply-time and
    # again at checkout.
    starts_at = models.DateTimeField(null=True, blank=True, default=None)
    ends_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        verbose_name = "Coupon"
        verbose_name_plural = "Coupons"
        constraints = [
            # Code unique per tenant — the natural lookup key.
            models.UniqueConstraint(
                fields=["tenant", "code"],
                name="uq_coupon_tenant_code",
            ),
            # Forward-compat scaffold: allows a future composite FK
            # (tenant_id, coupon_id) from redemption/cart tables — mirrors the
            # pattern on Cart (PROJECT_SPEC §3.2 / uq_cart_tenant_id).
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_coupon_tenant_id",
            ),
            # Belt-and-suspenders on TextChoices.
            models.CheckConstraint(
                check=Q(discount_type__in=["PERCENTAGE", "FIXED"]),
                name="ck_coupon_type_valid",
            ),
            # Discount value must always be positive.
            models.CheckConstraint(
                check=Q(value__gt=Decimal("0")),
                name="ck_coupon_value_pos",
            ),
            # Percentage discounts cannot exceed 100 %.
            # The expression reads: if type is not FIXED then value ≤ 100.
            models.CheckConstraint(
                check=Q(discount_type="FIXED") | Q(value__lte=Decimal("100")),
                name="ck_coupon_pct_lte_100",
            ),
            # used_count must not exceed usage_limit when a limit is set.
            models.CheckConstraint(
                check=Q(usage_limit__isnull=True) | Q(used_count__lte=F("usage_limit")),
                name="ck_coupon_used_within_limit",
            ),
            # When currency is set it must be a valid ISO 4217 3-letter code.
            models.CheckConstraint(
                check=Q(currency__isnull=True) | Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_coupon_currency_iso4217",
            ),
            # FIXED coupons must carry a currency — percentage ones must not
            # (to prevent accidental mismatches in multi-currency tenants).
            models.CheckConstraint(
                check=(
                    Q(discount_type="PERCENTAGE", currency__isnull=True)
                    | Q(discount_type="FIXED", currency__isnull=False)
                ),
                name="ck_coupon_fixed_requires_currency",
            ),
            # ends_at ≥ starts_at when both are provided.
            models.CheckConstraint(
                check=(
                    Q(ends_at__isnull=True)
                    | Q(starts_at__isnull=True)
                    | Q(ends_at__gte=F("starts_at"))
                ),
                name="ck_coupon_window_valid",
            ),
        ]
        indexes = [
            # (tenant, code) lookup is covered by the unique constraint above;
            # no duplicate index needed.
            #
            # Admin / expiry-sweep: "active coupons for this tenant ordered by
            # expiry date" — the most common ops query pattern.
            models.Index(
                fields=["tenant", "is_active", "ends_at"],
                name="ix_coupon_tenant_active_ends",
            ),
            # Supports soft-expiry queries: "which coupons expire before date X
            # for a given tenant?" — used by the Celery expiry sweep job.
            models.Index(
                fields=["tenant", "ends_at"],
                name="ix_coupon_tenant_ends",
            ),
        ]

    def __str__(self) -> str:
        return f"Coupon {self.code} ({self.discount_type})"
