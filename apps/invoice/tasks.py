"""Invoice Celery tasks.

Implements PROJECT_SPEC §4.6 — Celery-driven async invoice generation on
the ``invoices`` queue.

``generate_invoice`` is the task dispatched by ``PaymentService`` via
``transaction.on_commit`` after a successful payment authorisation.  The task
is a thin infrastructure wrapper that:

1. Fetches the ``Order`` row cross-tenant (the task is internal, runs in a
   trusted worker) to activate the correct tenant context.
2. Activates the tenant context so every ORM query inside is tenant-scoped.
3. Delegates to ``InvoiceService.generate_invoice_for_order`` — all business
   logic lives there, not in the task.
4. Returns a summary dict for Celery result storage.

Idempotency: ``InvoiceService.generate_invoice_for_order`` has an internal
fast-path guard — if an invoice already exists for the order, it returns
immediately.  The task's own status check at step 1 is a cheap short-circuit.

Retry strategy: ``autoretry_for=(Exception,)`` with exponential back-off +
jitter, max 5 attempts.  The service handles ``IntegrityError`` internally;
the task retries on any other exception (transient DB/storage failures).
"""

from __future__ import annotations

import logging
from uuid import UUID

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    queue="invoices",
    max_retries=5,
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
)
def generate_invoice(self, order_id: str) -> dict:
    """Generate an invoice for the given order asynchronously.

    Args:
        order_id: UUID string of the ``Order`` row to invoice.

    Returns:
        A dict with ``order_id``, ``invoice_id``, and ``invoice_number`` for
        Celery result storage.

    Side effects:
        Delegates all state mutations to ``InvoiceService.generate_invoice_for_order``.
    """
    from apps.invoice.services import InvoiceService
    from apps.order.models import Order
    from apps.tenant.context import tenant_context

    order_uuid = UUID(order_id)

    # ------------------------------------------------------------------
    # Load the order cross-tenant to resolve the tenant context.
    # ------------------------------------------------------------------
    try:
        order = (
            Order.objects.all_tenants()
            .select_related("tenant")
            .get(pk=order_uuid)
        )
    except Order.DoesNotExist:
        logger.error(
            "generate_invoice: Order %s not found — skipping.",
            order_id,
        )
        return {"order_id": order_id, "status": "not_found"}

    # ------------------------------------------------------------------
    # Delegate to the service layer inside the correct tenant context.
    # ------------------------------------------------------------------
    with tenant_context(order.tenant):
        try:
            invoice = InvoiceService().generate_invoice_for_order(order_uuid)
            logger.info(
                "generate_invoice: invoice #%s (%s) generated for order %s",
                invoice.number,
                invoice.id,
                order_id,
            )
            return {
                "order_id": order_id,
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.number,
                "status": "generated",
            }
        except Exception as exc:
            logger.error(
                "generate_invoice: failed for order %s: %s",
                order_id,
                exc,
            )
            raise self.retry(exc=exc)


def enqueue_generate_invoice(order_id: UUID) -> None:
    """Dispatch ``generate_invoice`` to the Celery ``invoices`` queue.

    This is the function referenced by ``PaymentService`` via
    ``transaction.on_commit``.  Wrapping the task dispatch in a named
    function keeps ``PaymentService`` independent of Celery imports — tests
    can replace this with a no-op lambda.

    Must be called inside ``transaction.on_commit(...)`` so the task is only
    enqueued after a successful commit.  A rolled-back payment must never
    produce an invoice task.
    """
    generate_invoice.apply_async(
        args=[str(order_id)],
        queue="invoices",
    )
