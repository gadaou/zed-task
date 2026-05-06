"""Payment models — ``PaymentMethod``, ``Payment``.

Implements PROJECT_SPEC §3.3 (pluggable ``PaymentGateway`` protocol), §4.6
(Celery-driven async authorization), and §5.3 (``PaymentIntent`` finite state
machine).

``PaymentMethod`` (existing stub, unchanged):
    A stored, reusable payment instrument belonging to a customer within a
    tenant (card token, bank account reference, …).  Full fields land with the
    checkout / payment-method-management iteration.

``Payment`` (new):
    A single payment attempt for a cart.  The ``status`` column is the FSM
    described in PROJECT_SPEC §5.3.  State transitions are enforced at the
    application layer by the payment service; a ``PaymentTransition`` allow-list
    table for DB-level FSM enforcement is on the roadmap (see ADR-NOTE below).

Design decisions:
- ``provider`` is a gateway slug (free-text string matching the
  ``PaymentGateway.slug`` attribute in the registry).  It is deliberately NOT a
  FK — gateway identities live in code, not in the DB.  Adding a new gateway
  requires no schema change.
- ``cart`` FK uses ``on_delete=PROTECT``.  A cart that has ever had a payment
  attempt must not be silently dropped; tenant deprovisioning logic must clean
  payments first.
- Money is ``Decimal`` + explicit ISO 4217 ``currency`` (PROJECT_SPEC §3.5).
  ``amount`` is the amount that was (or will be) charged; it is snapshotted at
  payment-creation time so that a subsequent cart edit or coupon removal does
  not retroactively change the payment record.
- ``failure_reason`` surfaces the gateway error message on ``FAILED`` status.
  It is intentionally a short CharField (255), not a TextField, because
  structured error detail belongs in the gateway event log, not the payment row.

ADR-NOTE — PaymentTransition allow-list table (PROJECT_SPEC §5.3):
    The spec calls for a DB-level CHECK constraint backed by an allow-list table
    to reject illegal FSM transitions.  That table lands with the checkout
    iteration when the full transition matrix is exercised end-to-end.  For now
    the ck_payment_status_valid CHECK constrains the value to the seven valid
    states, and the application-layer service is the gatekeeper for transitions.

ADR-NOTE — Reserved future columns (not migrated yet, documented here):
    - ``gateway_authorization_id CharField(255, blank=True)``
    - ``gateway_capture_id CharField(255, blank=True)``
    - ``idempotency_key UUIDField(null=True)`` — ties the payment to the
      checkout Idempotency-Key (PROJECT_SPEC §4.5)
    - ``last_attempt_at DateTimeField(null=True)``
    - ``attempts PositiveSmallIntegerField(default=0)``
    These columns are cheap additions (nullable or defaulted) that land without
    a table rewrite per PROJECT_SPEC §6.6.

ADR-NOTE — PaymentMethod ↔ Payment decoupling:
    A Payment may be attempted with a one-off gateway without a stored
    PaymentMethod (e.g. guest checkout with a single-use token).  ``provider``
    duplicates the gateway slug rather than deriving it from a PaymentMethod FK
    so that the payment record is self-contained and readable without a JOIN.

ADR-NOTE §6.2: UUID primary keys use ``uuid.uuid4`` in this iteration.  Replace
with ``uuid7()`` from common/ids.py when that module lands.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models
from django.db.models import Q

from apps.cart.models import Cart
from apps.tenant.models import TenantAwareModel


class PaymentMethod(TenantAwareModel):
    """A stored payment method belonging to a single tenant.

    Placeholder — real fields (gateway_slug, customer reference, tokenised
    instrument, metadata, last4, expiry, …) land in a subsequent iteration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # ADR-NOTE: gateway_slug is kept as the only field because it will remain
    # in the final model as the discriminator.  Other fields come later.
    gateway_slug = models.CharField(max_length=50, default="mock")

    class Meta:
        verbose_name = "Payment Method"
        verbose_name_plural = "Payment Methods"

    def __str__(self) -> str:
        return f"PaymentMethod {self.id} ({self.gateway_slug})"


class Payment(TenantAwareModel):
    """A single payment attempt for a cart, tracking gateway state.

    ``status`` follows the PROJECT_SPEC §5.3 finite state machine:

        REQUIRES_CONFIRMATION
            ↓ (gateway authorise call succeeds)
        AUTHORIZED
            ↓ (capture call, typically immediate for card-present)
        CAPTURED
            ↓ (webhook / polling confirms final settlement)
        SUCCEEDED          ← terminal success
        FAILED             ← terminal failure (gateway declined / error)
        CANCELLED          ← terminal (customer or merchant voided)
        REFUNDED           ← terminal (post-capture refund issued)

    Illegal transitions are enforced by the payment service.  The
    PaymentTransition allow-list table for DB-level FSM enforcement lands with
    the checkout iteration (see module ADR-NOTE).
    """

    class Status(models.TextChoices):
        REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION", "Requires confirmation"
        AUTHORIZED = "AUTHORIZED", "Authorized"
        CAPTURED = "CAPTURED", "Captured"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"
        REFUNDED = "REFUNDED", "Refunded"

    # ADR-NOTE §6.2: uuid4 → uuid7, same note as all other models.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Cart this payment is for.  PROTECT: a cart with a payment history must
    # not be silently deleted.  related_name allows cart.payments.all().
    cart = models.ForeignKey(
        Cart,
        on_delete=models.PROTECT,
        related_name="payments",
    )

    # Gateway slug from the PaymentGateway registry (PROJECT_SPEC §3.3).
    # Examples: "mock", "stripe", "hyperpay", "tabby".
    # Intentionally a string, not an FK — see module ADR-NOTE.
    provider = models.CharField(max_length=50)

    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.REQUIRES_CONFIRMATION,
    )

    # Amount snapshotted at payment creation — see module design decisions.
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    # ISO 4217 currency code; must match the cart's currency at creation time.
    # Enforced by DB CHECK ck_payment_currency_iso4217.
    currency = models.CharField(max_length=3)

    # Short human-readable reason populated by the gateway on FAILED.
    # Intentionally 255 chars — structured detail lives in the gateway event log.
    failure_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Payment"
        verbose_name_plural = "Payments"
        constraints = [
            # Amount must never be negative.
            models.CheckConstraint(
                check=Q(amount__gte=Decimal("0")),
                name="ck_payment_amount_nonneg",
            ),
            # Currency must be valid ISO 4217 three-letter code.
            models.CheckConstraint(
                check=Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_payment_currency_iso4217",
            ),
            # Belt-and-suspenders status guard matching the FSM.
            models.CheckConstraint(
                check=Q(
                    status__in=[
                        "REQUIRES_CONFIRMATION",
                        "AUTHORIZED",
                        "CAPTURED",
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "REFUNDED",
                    ]
                ),
                name="ck_payment_status_valid",
            ),
            # Forward-compat composite FK scaffold — mirrors the pattern on
            # Cart and Coupon (PROJECT_SPEC §3.2).
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_payment_tenant_id",
            ),
        ]
        indexes = [
            # Primary access pattern: "list all payments for a cart" —
            # used for retry display, refund flow, and audit log.
            models.Index(
                fields=["tenant", "cart"],
                name="ix_payment_tenant_cart",
            ),
            # Ops queue: "all payments stuck in REQUIRES_CONFIRMATION for
            # this tenant" — fed into the Celery polling loop (PROJECT_SPEC §4.6).
            models.Index(
                fields=["tenant", "status"],
                name="ix_payment_tenant_status",
            ),
            # Per-gateway health drill-down + circuit-breaker introspection:
            # "failure rate for provider X in this tenant over the last hour".
            models.Index(
                fields=["tenant", "provider", "status"],
                name="ix_payment_tenant_prov_status",
            ),
            # Terminal-state sweep / aging: "payments that reached SUCCEEDED
            # more than N days ago" — used by archival and invoice generation.
            models.Index(
                fields=["tenant", "status", "updated_at"],
                name="ix_payment_tenant_status_upd",
            ),
        ]

    def __str__(self) -> str:
        return f"Payment {self.id} [{self.status}] via {self.provider}"
