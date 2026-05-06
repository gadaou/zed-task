"""Cart models — ``Cart``, ``CartItem``.

Implements PROJECT_SPEC §3.2 (tenant isolation), §3.4 (optimistic ``version``
column on Cart, ``select_for_update`` during checkout), §3.5 (Decimal money +
ISO 4217 currency), §6.2 (UUID primary keys).

Design decisions:
- ``Cart.user_id`` is a bare UUIDField — no FK to ``auth.User`` — to keep the
  cart aggregate decoupled from Django's auth subsystem (PROJECT_SPEC §8:
  identity provider / SSO integration is out of scope).
- ``CartItem.product_id`` is a bare UUIDField — no FK to ``catalog.Product``
  — to keep the cart aggregate decoupled from the catalog app. Catalog lookups
  (price/stock validation) happen in the service layer at add-time and again
  at checkout (PROJECT_SPEC §5.3).
- ``CartItem.price_snapshot`` captures the product price at the moment the
  item was added. The live catalog price may drift; checkout re-validates and
  surfaces ``product/price-changed`` if the delta is outside tolerance.
- ``Cart.version`` is the optimistic concurrency token (PROJECT_SPEC §3.4).
  Service-layer updates include ``WHERE version = :v AND UPDATE version = v+1``;
  zero rows affected raises ``cart/stale-version`` (HTTP 409).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.tenant.models import TenantAwareModel


class Cart(TenantAwareModel):
    # Forward reference — the actual FK targets are declared in other apps so
    # we use string labels to avoid circular imports.  Both are nullable because
    # a cart is created before the customer has chosen an address or payment
    # method; they are set by the add-address / add-payment-method actions and
    # consumed at checkout time.
    """A mutable, per-customer, per-tenant shopping cart.

    Lifecycle: ``ACTIVE`` → (checkout service) → ``CHECKED_OUT``.
    A checked-out cart is effectively immutable; a new cart is created for
    subsequent purchases.

    ``total_price`` is a denormalized sum of (quantity × price_snapshot) over
    all CartItems. It is updated by the service layer on every add/remove/update
    operation to avoid an aggregation query on every cart read.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CHECKED_OUT = "CHECKED_OUT", "Checked out"

    # ADR-NOTE §6.2: UUIDv7 (time-ordered) is preferred for B-tree performance
    # at scale. A ``uuid7()`` helper will be introduced in a common ids module
    # in a later iteration; ``uuid.uuid4`` is used in the interim.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Loose reference to the customer; no FK to auth.User (see module docstring).
    user_id = models.UUIDField()

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    # Denormalized running total. Always non-negative; enforced by CHECK below.
    total_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    # Sum of every applied coupon's discount snapshot (see apps.coupon.CartCoupon).
    # Kept in sync by ``recalculate_cart`` after every cart or coupon mutation;
    # always non-negative (CHECK ck_cart_discount_nonneg).  PROJECT_SPEC §5.3
    # mandates that checkout re-evaluates every constraint and may recompute
    # this — the apply-time snapshot is a UX optimisation, not the source of
    # truth at finalisation.
    discount_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    # Denormalized payable: total_price - discount_amount, clamped at zero so
    # over-large fixed coupons cannot push the cart negative.  Stored (rather
    # than computed on read) to keep cart-read responses cheap (PROJECT_SPEC §5
    # SLO: GET cart p99 < 300 ms).
    total_after_discount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    # ISO 4217 three-letter currency code. Must match the currency on every
    # CartItem in this cart; enforced by the service layer and CartItem.clean().
    currency = models.CharField(max_length=3, default="USD")

    # Optimistic concurrency token (PROJECT_SPEC §3.4).
    # Incremented on every mutating write. Update statements compare
    # ``WHERE id = %s AND version = %s`` and raise if zero rows affected.
    version = models.PositiveIntegerField(default=0)

    # The customer's selected shipping address for this cart.  Set by the
    # POST /cart/add-address action; required by POST /cart/checkout.
    selected_address = models.ForeignKey(
        "addresses.Address",
        null=True,
        blank=True,
        default=None,
        on_delete=models.PROTECT,
        related_name="+",
    )

    # The customer's selected payment method for this cart.  Set by the
    # POST /cart/add-payment-method action; required by POST /cart/checkout.
    selected_payment_method = models.ForeignKey(
        "payment.PaymentMethod",
        null=True,
        blank=True,
        default=None,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        verbose_name = "Cart"
        verbose_name_plural = "Carts"
        constraints = [
            models.CheckConstraint(
                check=Q(total_price__gte=0),
                name="ck_cart_total_nonneg",
            ),
            # discount_amount is a sum of non-negative discount snapshots.
            models.CheckConstraint(
                check=Q(discount_amount__gte=0),
                name="ck_cart_discount_nonneg",
            ),
            # Payable can never be negative — clamped by recalculate_cart even
            # when fixed coupons exceed the subtotal.
            models.CheckConstraint(
                check=Q(total_after_discount__gte=0),
                name="ck_cart_total_after_discount_nonneg",
            ),
            # Belt-and-suspenders status guard in addition to TextChoices.
            # String literals used here because Status is an inner class and
            # is not yet in scope when Meta is evaluated by the metaclass.
            models.CheckConstraint(
                check=Q(status__in=["ACTIVE", "CHECKED_OUT"]),
                name="ck_cart_status_valid",
            ),
            models.CheckConstraint(
                check=Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_cart_currency_iso4217",
            ),
            # Composite unique on (tenant, id) so CartItem can eventually use a
            # composite FK (tenant_id, cart_id) to enforce same-tenant ownership
            # at the DB level (PROJECT_SPEC §3.2 / §9.7 RLS roadmap).
            # ADR-NOTE: with the current simple FK the uniqueness is already
            # guaranteed by PK; this constraint is forward-compatible scaffolding.
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_cart_tenant_id",
            ),
        ]
        indexes = [
            # Primary access pattern: "give me all carts for tenant + user".
            models.Index(
                fields=["tenant", "user_id"],
                name="ix_cart_tenant_user",
            ),
            # Admin / ops pattern: "show active carts for this tenant".
            models.Index(
                fields=["tenant", "status"],
                name="ix_cart_tenant_status",
            ),
            # Combined index for the common service query: fetch the active cart
            # for a specific user within a tenant in one index range scan.
            models.Index(
                fields=["tenant", "user_id", "status"],
                name="ix_cart_tenant_user_status",
            ),
        ]

    def __str__(self) -> str:
        return f"Cart {self.id} [{self.status}]"


class CartItem(TenantAwareModel):
    """A line item within a Cart, recording quantity and a price snapshot.

    Each (cart, product_id) pair is unique — adding the same product twice
    increases ``quantity`` rather than inserting a second row (enforced by the
    service layer and the ``uq_cartitem_cart_product`` constraint below).

    ``tenant`` is inherited from TenantAwareModel and must always equal the
    parent Cart's tenant. ``clean()`` enforces this at the application layer;
    an ADR-NOTE documents the roadmap for a DB-level composite FK guard.
    """

    # ADR-NOTE §6.2: same uuid4→uuid7 migration note as Cart.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="items",
    )

    # Loose reference to catalog.Product; no FK (see module docstring).
    product_id = models.UUIDField()

    # Must be >= 1; enforced by CheckConstraint below and validated in clean().
    quantity = models.PositiveIntegerField()

    # Price captured at the moment the item was added (PROJECT_SPEC §5.3).
    # Preserved even if the catalog price changes later; checkout re-validates.
    price_snapshot = models.DecimalField(max_digits=12, decimal_places=2)

    # Currency of price_snapshot; must match Cart.currency (service-layer rule).
    currency = models.CharField(max_length=3, default="USD")

    class Meta:
        verbose_name = "Cart Item"
        verbose_name_plural = "Cart Items"
        constraints = [
            # One line per product per cart. Adding the same product again
            # should update quantity, not create a duplicate row.
            models.UniqueConstraint(
                fields=["cart", "product_id"],
                name="uq_cartitem_cart_product",
            ),
            models.CheckConstraint(
                check=Q(quantity__gte=1),
                name="ck_cartitem_qty_pos",
            ),
            models.CheckConstraint(
                check=Q(price_snapshot__gte=0),
                name="ck_cartitem_price_nonneg",
            ),
            models.CheckConstraint(
                check=Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_cartitem_currency_iso4217",
            ),
        ]
        indexes = [
            # Supports "list all items in a cart scoped to tenant" —
            # the most frequent CartItem query issued by the service layer.
            models.Index(
                fields=["tenant", "cart"],
                name="ix_cartitem_tenant_cart",
            ),
            # Supports "find all carts containing a specific product" —
            # used for catalog-side impact analysis (e.g. product deactivation).
            models.Index(
                fields=["tenant", "product_id"],
                name="ix_cartitem_tenant_product",
            ),
        ]

    def clean(self) -> None:
        """Enforce same-tenant invariant between CartItem and its parent Cart.

        PROJECT_SPEC §3.2: "Foreign keys are validated to be same-tenant."
        The TenantAwareManager auto-stamps ``tenant`` from the context, but a
        caller could theoretically assign a Cart belonging to a different tenant.
        This guard catches that at validation time.

        ADR-NOTE: A DB-level same-tenant constraint would require a composite
        FK (tenant_id, cart_id) → cart(tenant_id, id). The UniqueConstraint
        uq_cart_tenant_id on Cart is the scaffolding for that future step.
        For now the application-layer check is the enforcement point.
        """
        super().clean()
        if self.cart_id and self.tenant_id:
            # Use the in-memory Cart object if it is already loaded (avoids an
            # extra DB round-trip); otherwise issue a targeted query that bypasses
            # tenant scoping via all_tenants() — we are deliberately reading a
            # cross-tenant value to compare, not to expose data.
            if "cart" in self.__dict__:
                cart_tenant_id = self.__dict__["cart"].tenant_id
            else:
                cart_tenant_id = (
                    Cart.objects.all_tenants()
                    .filter(pk=self.cart_id)
                    .values_list("tenant_id", flat=True)
                    .first()
                )
            if cart_tenant_id is not None and str(cart_tenant_id) != str(self.tenant_id):
                raise ValidationError(
                    "CartItem tenant must match its parent Cart tenant. "
                    f"Expected {cart_tenant_id}, got {self.tenant_id}."
                )
        if self.quantity is not None and self.quantity < 1:
            raise ValidationError({"quantity": "Quantity must be at least 1."})

    def __str__(self) -> str:
        return f"CartItem {self.product_id} ×{self.quantity} (cart {self.cart_id})"
