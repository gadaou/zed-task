"""Invoice services — ``InvoiceService``.

Implements PROJECT_SPEC §2 bonus: Invoice handling.

Public API
----------
``InvoiceService`` class with one public method:

    ``generate_invoice_for_order(order_id: UUID) -> Invoice``

Algorithm (two-phase design — DB locks held for DB work only):

    1. Idempotency check:
       a. If ``Invoice`` exists **and** ``pdf_url != ""`` → fully generated;
          return immediately (fast path, no locks).
       b. If ``Invoice`` exists **and** ``pdf_url == ""`` → the row was created
          by a previous call that crashed before (or during) PDF rendering.
          Reload the ``Order`` for context and jump straight to step 4
          (no sequence re-allocation — the number is already committed).
       c. If ``Invoice`` does not exist → proceed to step 2.
    2. Load and validate the ``Order`` (cross-tenant; raises ``OrderNotFound``
       or ``OrderNotPaid`` on bad input).
    3. ``transaction.atomic()`` — pure DB work, no file I/O:
       a. ``SELECT FOR UPDATE`` the Order row (serialises concurrent tasks).
       b. ``SELECT FOR UPDATE`` the ``InvoiceSequence`` row
          (``get_or_create`` under the lock).
       c. Increment ``last_number``; save — gap-free monotonic allocation.
       d. Compute taxes = ``order.total × 15%`` (ROUND_HALF_UP, 2 dp).
       e. ``Invoice.objects.create(..., pdf_url="")`` — **no file I/O here**.
          The ``OneToOneField`` constraint makes a duplicate insert an
          ``IntegrityError``, caught in the outer retry block.
    4. Render PDF **outside the transaction** (file I/O after DB commit).
       Using ``invoice.id`` that was assigned in step 3.
    5. Status-guarded ``UPDATE Invoice SET pdf_url=… WHERE pk=… AND pdf_url=""``.
       The ``WHERE pdf_url=""`` guard makes a concurrent winner's write safe —
       only the first writer commits; subsequent ones are no-ops.
    6. ``invoice.refresh_from_db()`` → return.

Why no I/O inside the transaction:
    DB transactions hold row locks for their duration.  Filesystem writes can
    take O(10 ms–100 ms) and can stall under I/O pressure.  Holding
    ``InvoiceSequence`` and ``Order`` row locks during that window increases
    contention under concurrent load and can cascade into lock-wait timeouts.
    Splitting the work means the sequence-allocation lock is held for only
    microseconds of pure DB work.

Idempotency:
    - ``pdf_url != ""`` → return (normal fast path after full success).
    - ``pdf_url == ""`` → skip allocation, retry PDF only.
    - ``IntegrityError`` on INSERT → concurrent task won the race; re-fetch
      the existing row and proceed to the PDF step (or return if it already
      has a url).
    - PDF ``UPDATE`` uses ``WHERE pdf_url=""`` so a concurrent writer's url
      is never overwritten.

Tax rate:
    ADR-NOTE: flat 15% until ``Tenant.tax_rate`` configuration lands.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.core.logging import log_event
from apps.invoice.exceptions import OrderNotFound, OrderNotPaid

logger = logging.getLogger(__name__)

TAX_RATE = Decimal("0.15")


class InvoiceService:
    """Service for generating invoices for paid orders.

    Stateless — safe to instantiate per-call or as a singleton.
    """

    def generate_invoice_for_order(self, order_id: UUID) -> "Invoice":  # type: ignore[name-defined]
        """Generate (or idempotently return) the invoice for ``order_id``.

        Args:
            order_id: UUID of the ``Order`` to invoice.  The order must be
                      in ``PAID`` status; the Celery task ensures this by
                      only dispatching after the payment succeeds.

        Returns:
            The ``Invoice`` instance (new or existing) with ``pdf_url`` set.

        Raises:
            OrderNotFound:  No order with ``order_id`` exists.
            OrderNotPaid:   Order exists but is not in ``PAID`` status.
        """
        from apps.invoice.models import Invoice
        from apps.order.models import Order

        # ------------------------------------------------------------------
        # Step 1 — Idempotency check (no locks, no file I/O).
        # ------------------------------------------------------------------
        try:
            invoice = Invoice.objects.get(order_id=order_id)
        except Invoice.DoesNotExist:
            invoice = None

        if invoice is not None:
            if invoice.pdf_url:
                # Fully generated on a previous call — fast path return.
                log_event(
                    logger,
                    "invoice.generated",
                    outcome="skipped",
                    invoice_id=str(invoice.id),
                    order_id=str(order_id),
                    reason="already_complete",
                )
                return invoice

            # pdf_url is empty: DB row was committed but PDF rendering failed.
            # Load the order for rendering context and jump to step 4.
            log_event(
                logger,
                "invoice.pdf_retry",
                invoice_id=str(invoice.id),
                order_id=str(order_id),
            )
            order = (
                Order.objects.all_tenants()
                .select_related("tenant")
                .get(pk=order_id)
            )
        else:
            # ------------------------------------------------------------------
            # Step 2 — Load and validate the order.
            # ------------------------------------------------------------------
            try:
                order = (
                    Order.objects.all_tenants()
                    .select_related("tenant")
                    .get(pk=order_id)
                )
            except Order.DoesNotExist:
                raise OrderNotFound(f"order {order_id} not found")

            if order.status != "PAID":
                raise OrderNotPaid(
                    f"order {order_id} is in status '{order.status}'; expected PAID"
                )

            # ------------------------------------------------------------------
            # Step 3 — Atomic DB work: allocate sequence number, create row.
            #          No file I/O inside this transaction.
            # ------------------------------------------------------------------
            try:
                invoice = self._create_invoice_row_atomic(order)
            except IntegrityError:
                # A concurrent task raced past step 1 and won the INSERT.
                log_event(
                    logger,
                    "invoice.concurrent_insert",
                    order_id=str(order_id),
                    reason="IntegrityError",
                )
                invoice = Invoice.objects.get(order_id=order_id)
                if invoice.pdf_url:
                    # The concurrent task also finished the PDF — nothing to do.
                    return invoice

        # ------------------------------------------------------------------
        # Step 4 — Render PDF outside the transaction (file I/O after commit).
        # ------------------------------------------------------------------
        from apps.invoice.pdf import render_invoice_pdf

        pdf_url = render_invoice_pdf(
            invoice_id=invoice.id,
            order_id=order.id,
            invoice_number=invoice.number,
            tenant_name=order.tenant.name,
            total=invoice.total,
            taxes=invoice.taxes,
            currency=invoice.currency,
        )

        # ------------------------------------------------------------------
        # Step 5 — Status-guarded update: only writes if pdf_url is still "".
        #          Concurrent winners' urls are never overwritten.
        # ------------------------------------------------------------------
        Invoice.objects.filter(pk=invoice.pk, pdf_url="").update(pdf_url=pdf_url)
        invoice.refresh_from_db()

        log_event(
            logger,
            "invoice.generated",
            outcome="success",
            invoice_id=str(invoice.id),
            order_id=str(order_id),
        )
        return invoice

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @transaction.atomic
    def _create_invoice_row_atomic(self, order: "Order") -> "Invoice":  # type: ignore[name-defined]
        """Allocate a sequence number and insert the Invoice row.

        Pure DB work — no file I/O.  Must be called with the tenant context
        already active and outside any outer transaction (so that
        ``transaction.on_commit`` hooks registered by the caller fire after
        this savepoint commits, not after the outer transaction commits).

        Returns the newly created ``Invoice`` with ``pdf_url=""``.
        """
        from apps.invoice.models import Invoice, InvoiceSequence

        # Lock the order to serialise concurrent invoice tasks for the same order.
        from apps.order.models import Order

        Order.objects.all_tenants().select_for_update().get(pk=order.pk)

        # Lock-or-create the per-tenant sequence row and increment atomically.
        seq, _ = (
            InvoiceSequence.objects.select_for_update()
            .get_or_create(tenant=order.tenant)
        )
        seq.last_number += 1
        seq.save(update_fields=["last_number", "updated_at"])
        number = seq.last_number

        # Compute taxes (flat rate — see module ADR-NOTE).
        taxes = (order.total * TAX_RATE).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # Insert the row with an empty pdf_url placeholder.
        # The OneToOneField on order makes a duplicate insert raise IntegrityError,
        # caught by the caller.
        invoice = Invoice.objects.create(
            tenant=order.tenant,
            order=order,
            number=number,
            total=order.total,
            taxes=taxes,
            currency=order.currency,
            pdf_url="",  # filled in after the transaction commits
        )

        return invoice
