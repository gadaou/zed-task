"""Integration tests for the ``generate_invoice`` Celery task.

Coverage:
    test_task_generates_invoice_for_paid_order
        — Dispatching the task for a PAID order creates the Invoice.
    test_task_queue_name
        — The task is bound to the ``invoices`` queue.
    test_task_idempotent_redelivery
        — Calling the task twice for the same order creates exactly one Invoice.
    test_task_order_not_found
        — Dispatching for a non-existent order_id returns ``status: not_found``.
    test_enqueue_generate_invoice_dispatches_task
        — ``enqueue_generate_invoice`` calls ``apply_async`` with the correct args.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.invoice.tasks import enqueue_generate_invoice, generate_invoice


# ---------------------------------------------------------------------------
# Happy path — task creates the invoice
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_task_generates_invoice_for_paid_order(
    paid_order_factory, tenant, settings, tmp_path
):
    """Dispatching generate_invoice for a PAID order creates the Invoice row."""
    settings.MEDIA_ROOT = str(tmp_path)

    order = paid_order_factory(total=Decimal("80.00"), currency="USD")
    result = generate_invoice(str(order.id))

    assert result["status"] == "generated"
    assert result["order_id"] == str(order.id)
    assert result["invoice_number"] == 1

    from apps.invoice.models import Invoice

    invoice = Invoice.objects.get(order_id=order.id)
    assert invoice.total == Decimal("80.00")


# ---------------------------------------------------------------------------
# Queue binding
# ---------------------------------------------------------------------------


def test_task_queue_name():
    """The generate_invoice task must be bound to the ``invoices`` queue."""
    assert generate_invoice.queue == "invoices"


# ---------------------------------------------------------------------------
# Idempotency under re-delivery
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_task_idempotent_redelivery(paid_order_factory, tenant, settings, tmp_path):
    """Invoking the task twice for the same order creates exactly one Invoice."""
    settings.MEDIA_ROOT = str(tmp_path)

    order = paid_order_factory(total=Decimal("40.00"))
    result1 = generate_invoice(str(order.id))
    result2 = generate_invoice(str(order.id))

    assert (
        result1["invoice_id"] == result2["invoice_id"]
    ), "Both task invocations must return the same invoice_id"

    from apps.invoice.models import Invoice

    assert Invoice.objects.filter(order_id=order.id).count() == 1


# ---------------------------------------------------------------------------
# Order not found
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_task_order_not_found(tenant):
    """Dispatching for a non-existent order_id returns status=not_found."""
    result = generate_invoice(str(uuid.uuid4()))
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# enqueue_generate_invoice dispatcher
# ---------------------------------------------------------------------------


def test_enqueue_generate_invoice_dispatches_task():
    """enqueue_generate_invoice calls apply_async with the correct args and queue."""
    order_id = uuid.uuid4()

    with patch.object(generate_invoice, "apply_async") as mock_apply:
        enqueue_generate_invoice(order_id)

    mock_apply.assert_called_once_with(
        args=[str(order_id)],
        queue="invoices",
    )
