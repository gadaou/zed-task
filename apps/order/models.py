"""Order models — ``Order``, ``OrderItem``.

Implements PROJECT_SPEC §2 operation 7 (checkout) and §5.3 (immutable record
of a successful checkout).

Design decisions:
- ``Order`` is immutable once created.  The only field that changes after
  creation is ``status`` (via payment events delivered by Celery workers or
  webhooks).
- ``Order.cart`` is a PROTECT FK — historical link to the cart that produced
  this order.  Kept for audit and customer support ("show me the cart that
  led to this order"), never mutated.
- ``Order.address`` and ``Order.payment_method`` are PROTECT FKs — the
  customer's address and payment instrument at the time of checkout are
  retained for fulfilment and dispute resolution even if the customer later
  changes or removes them.
- ``Order.user_id`` is a bare UUIDField — no FK to ``auth.User``, matching
  the same pattern as ``Cart.user_id`` (PROJECT_SPEC §8: identity provider
  integration is out of scope).
- ``Order.idempotency_key`` is the client-supplied ``Idempotency-Key`` header
  value stamped on the order row.  It links the order to its
  ``IdempotencyRecord`` and allows idempotent replay to return the same
  ``order_id`` without re-querying the idempotency table.
- ``OrderItem.product_id`` is a bare UUIDField — no FK to
  ``catalog.Product``, matching ``CartItem.product_id``.
- Money fields are ``DecimalField`` + ISO 4217 ``currency`` (PROJECT_SPEC §3.5).

ADR-NOTE §6.2: UUID primary keys use ``uuid.uuid4`` in this iteration.
Replace with ``uuid7()`` from common/ids.py when that module lands.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models
from django.db.models import Q

from apps.addresses.models import Address
from apps.cart.models import Cart
from apps.payment.models import PaymentMethod
from apps.tenant.models import TenantAwareModel


class Order(TenantAwareModel):
    """Immutable record of a successful checkout.

    ``status`` follows a simple FSM:

        PENDING_PAYMENT  — checkout committed; payment authorisation pending.
            ↓ (Celery task: payment gateway confirms authorisation)
        PAID             — terminal success.
            ↓ (refund path)
        CANCELLED / FAILED ← terminal failure / explicit cancellation.

    The full payment FSM lives on ``Payment``; ``Order.status`` is the
    merchant-visible summary state.
    """

    class Status(models.TextChoices):
        PENDING_PAYMENT = "PENDING_PAYMENT", "Pending payment"
        PAID = "PAID", "Paid"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Loose reference to the customer.  No FK to auth.User (see module docstring).
    user_id = models.UUIDField()

    # Historical link to the originating cart.  PROTECT: an order must not be
    # silently lost when the cart is (hypothetically) deleted.
    cart = models.ForeignKey(
        Cart,
        on_delete=models.PROTECT,
        related_name="orders",
    )

    # Snapshot of the shipping address at checkout time.  PROTECT so address
    # deletion cannot wipe order history.
    address = models.ForeignKey(
        Address,
        on_delete=models.PROTECT,
        related_name="orders",
    )

    # Snapshot of the payment instrument used.  PROTECT for the same reason.
    payment_method = models.ForeignKey(
        PaymentMethod,
        on_delete=models.PROTECT,
        related_name="orders",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING_PAYMENT,
    )

    # Money snapshot — taken from cart.total_price, cart.discount_amount,
    # cart.total_after_discount at checkout time.  Never recalculated.
    subtotal = models.DecimalField(max_digits=14, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2)
    total = models.DecimalField(max_digits=14, decimal_places=2)

    # ISO 4217 three-letter currency code, copied from the cart.
    currency = models.CharField(max_length=3)

    # B2B snapshot fields — copied from Cart at checkout time.  Empty string
    # when the buyer did not supply business details (B2C flow).
    company_name = models.CharField(max_length=200, blank=True, default="")
    tax_number = models.CharField(max_length=50, blank=True, default="")
    purchase_order_reference = models.CharField(max_length=100, blank=True, default="")

    # Client-supplied Idempotency-Key header value (PROJECT_SPEC §4.5).
    # Indexed for fast idempotency-record → order lookup.
    idempotency_key = models.UUIDField(db_index=True)

    # Optimistic concurrency token.  Updated only on status transitions;
    # the order body (items, totals) never changes.
    version = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Order"
        verbose_name_plural = "Orders"
        constraints = [
            models.CheckConstraint(
                check=Q(status__in=["PENDING_PAYMENT", "PAID", "FAILED", "CANCELLED"]),
                name="ck_order_status_valid",
            ),
            models.CheckConstraint(
                check=Q(subtotal__gte=Decimal("0")),
                name="ck_order_subtotal_nonneg",
            ),
            models.CheckConstraint(
                check=Q(discount_amount__gte=Decimal("0")),
                name="ck_order_discount_nonneg",
            ),
            models.CheckConstraint(
                check=Q(total__gte=Decimal("0")),
                name="ck_order_total_nonneg",
            ),
            models.CheckConstraint(
                check=Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_order_currency_iso4217",
            ),
            # Forward-compat composite FK scaffold — mirrors the pattern on
            # Cart, Coupon, Address (PROJECT_SPEC §3.2).
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_order_tenant_id",
            ),
        ]
        indexes = [
            # Primary access pattern: "list orders for a user in this tenant."
            models.Index(
                fields=["tenant", "user_id", "created_at"],
                name="ix_order_tenant_user_created",
            ),
            # Ops / analytics: "all orders in a given status for this tenant."
            models.Index(
                fields=["tenant", "status"],
                name="ix_order_tenant_status",
            ),
            # Link back from cart to order (checkout audit, retry detection).
            models.Index(
                fields=["tenant", "cart"],
                name="ix_order_tenant_cart",
            ),
        ]

    def __str__(self) -> str:
        return f"Order {self.id} [{self.status}] total={self.total}{self.currency}"


class OrderItem(TenantAwareModel):
    """A line item snapshot within an Order.

    Immutable once created.  Mirrors ``CartItem`` but belongs to the order
    aggregate — the order is the source of truth for what was purchased at
    what price, regardless of subsequent catalog changes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
    )

    # Loose reference to catalog.Product (no FK — see module docstring).
    product_id = models.UUIDField()

    quantity = models.PositiveIntegerField()

    # Price at checkout time (the CartItem.price_snapshot value, re-validated
    # against the live catalog inside the checkout transaction).
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    # ISO 4217 currency code; must match Order.currency.
    currency = models.CharField(max_length=3)

    class Meta:
        verbose_name = "Order Item"
        verbose_name_plural = "Order Items"
        constraints = [
            # One line per product per order — mirrors the CartItem constraint.
            models.UniqueConstraint(
                fields=["order", "product_id"],
                name="uq_orderitem_order_product",
            ),
            models.CheckConstraint(
                check=Q(quantity__gte=1),
                name="ck_orderitem_qty_pos",
            ),
            models.CheckConstraint(
                check=Q(unit_price__gte=Decimal("0")),
                name="ck_orderitem_price_nonneg",
            ),
            models.CheckConstraint(
                check=Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_orderitem_currency_iso4217",
            ),
        ]
        indexes = [
            # Most frequent query: "all items for a given order."
            models.Index(
                fields=["tenant", "order"],
                name="ix_orderitem_tenant_order",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"OrderItem {self.product_id} ×{self.quantity} @ {self.unit_price}"
            f" (order {self.order_id})"
        )
