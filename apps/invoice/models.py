"""Invoice models — ``InvoiceSequence``, ``Invoice``.

Implements PROJECT_SPEC §2 bonus: Invoice handling.

Design decisions:
- ``InvoiceSequence`` is a per-tenant counter that is incremented inside a
  ``SELECT FOR UPDATE`` transaction to guarantee gap-free monotonic invoice
  numbers without a Postgres sequence or advisory lock. This approach also
  works with the SQLite in-memory fallback used in tests
  (cart_system/settings/test.py).
- ``Invoice`` is a OneToOne to ``Order`` enforcing at the DB level that each
  order produces at most one invoice.  The service handles idempotency by
  catching IntegrityError on duplicate inserts (at-least-once Celery delivery).
- ``total`` and ``taxes`` are Decimal snapshots, never recalculated.
- ``pdf_url`` stores a relative URL under MEDIA_URL (e.g.
  ``/media/invoices/<invoice_id>.pdf``). Future iterations can swap the
  filesystem for S3 by changing ``default_storage`` in settings.
- ``generated_at`` is a dedicated field (rather than aliasing ``created_at``)
  to make invoice semantics self-documenting in the API and admin.

ADR-NOTE §6.2: UUID primary keys use ``uuid.uuid4`` in this iteration.
Replace with ``uuid7()`` from common/ids.py when that module lands.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models
from django.db.models import Q

from apps.tenant.models import TenantAwareModel


class InvoiceSequence(TenantAwareModel):
    """Per-tenant monotonic invoice number counter.

    There is at most one row per tenant (enforced by a UniqueConstraint).
    To allocate the next invoice number:

        seq = InvoiceSequence.objects.select_for_update().get_or_create(tenant=tenant)[0]
        seq.last_number += 1
        seq.save(update_fields=["last_number", "updated_at"])
        next_number = seq.last_number

    The ``SELECT FOR UPDATE`` must be inside a ``transaction.atomic()`` block.
    """

    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Invoice Sequence"
        verbose_name_plural = "Invoice Sequences"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"],
                name="uq_invoicesequence_tenant",
            ),
        ]

    def __str__(self) -> str:
        return f"InvoiceSequence tenant={self.tenant_id} last={self.last_number}"


class Invoice(TenantAwareModel):
    """Immutable financial document generated for every paid order.

    Created asynchronously by the Celery ``invoices`` queue after
    ``Order.status`` transitions to ``PAID``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # One invoice per order — enforced at the DB level.
    order = models.OneToOneField(
        "order.Order",
        on_delete=models.PROTECT,
        related_name="invoice",
    )

    # Monotonic, gap-free per-tenant display number.  Not a public API
    # identifier (PROJECT_SPEC §6.2 — no sequential IDs in the API).
    number = models.PositiveIntegerField()

    # Money snapshot — taken from Order.total at invoice-generation time.
    total = models.DecimalField(max_digits=14, decimal_places=2)

    # Computed taxes (15% flat — ADR-NOTE: replace with tenant tax config).
    taxes = models.DecimalField(max_digits=14, decimal_places=2)

    # ISO 4217 currency code, copied from Order.currency.
    currency = models.CharField(max_length=3)

    # Relative URL to the generated PDF under MEDIA_URL.
    # Empty string ("") means the DB row is committed but PDF has not yet been
    # rendered (two-phase generation: DB first, file I/O second).
    pdf_url = models.CharField(max_length=512, blank=True, default="")

    # Explicit timestamp for invoice-domain semantics (mirrors created_at).
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Invoice"
        verbose_name_plural = "Invoices"
        constraints = [
            # One invoice number per tenant — gaps are not allowed.
            models.UniqueConstraint(
                fields=["tenant", "number"],
                name="uq_invoice_tenant_number",
            ),
            # Composite forward-compat FK scaffold (mirrors Order).
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_invoice_tenant_id",
            ),
            models.CheckConstraint(
                check=Q(total__gte=Decimal("0")),
                name="ck_invoice_total_nonneg",
            ),
            models.CheckConstraint(
                check=Q(taxes__gte=Decimal("0")),
                name="ck_invoice_taxes_nonneg",
            ),
            models.CheckConstraint(
                check=Q(number__gte=1),
                name="ck_invoice_number_pos",
            ),
            models.CheckConstraint(
                check=Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_invoice_currency_iso4217",
            ),
        ]
        indexes = [
            # Primary access pattern: "invoice for this order."
            models.Index(
                fields=["tenant", "order"],
                name="ix_invoice_tenant_order",
            ),
        ]

    def __str__(self) -> str:
        return f"Invoice #{self.number} order={self.order_id} total={self.total}{self.currency}"
