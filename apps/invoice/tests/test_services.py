"""Integration tests for ``InvoiceService``.

Coverage:
    test_generate_invoice_creates_record
        — Happy path: a PAID order produces an Invoice with correct fields.
    test_invoice_taxes_calculation
        — taxes = total * 15% (ROUND_HALF_UP, 2 dp).
    test_invoice_pdf_file_exists
        — The PDF file is written under MEDIA_ROOT/invoices/.
    test_invoice_db_row_committed_before_pdf
        — Invoice row exists in DB (with pdf_url="") even if PDF rendering fails.
    test_invoice_pdf_failure_leaves_row_intact
        — If PDF rendering raises, the Invoice row remains with pdf_url="" and
          the sequence is not double-advanced.
    test_invoice_pdf_retry_succeeds
        — After a PDF failure (pdf_url=""), calling again renders the PDF and
          updates pdf_url without allocating a new sequence number.
    test_invoice_idempotent_full_success_no_rerender
        — Calling after a fully-generated invoice (pdf_url set) returns the
          same Invoice without calling render_invoice_pdf again.
    test_invoice_idempotent_double_call
        — Calling generate_invoice_for_order twice returns the same Invoice
          and leaves InvoiceSequence.last_number advanced exactly once.
    test_invoice_monotonic_numbers_same_tenant
        — Multiple orders in the same tenant get sequential numbers (1, 2, 3).
    test_invoice_numbering_resets_per_tenant
        — A different tenant's first invoice has number=1.
    test_order_not_found_raises
        — Passing a non-existent order_id raises OrderNotFound.
    test_order_not_paid_raises
        — Passing a PENDING_PAYMENT order raises OrderNotPaid.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.invoice.exceptions import OrderNotFound, OrderNotPaid
from apps.invoice.services import InvoiceService


# ---------------------------------------------------------------------------
# Happy path — basic creation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_generate_invoice_creates_record(
    paid_order_factory, tenant, settings, tmp_path
):
    """A PAID order produces an Invoice with correct fields."""
    settings.MEDIA_ROOT = str(tmp_path)

    order = paid_order_factory(total=Decimal("100.00"), currency="USD")
    invoice = InvoiceService().generate_invoice_for_order(order.id)

    assert invoice.order_id == order.id
    assert invoice.total == Decimal("100.00")
    assert invoice.currency == "USD"
    assert invoice.number == 1
    assert invoice.pdf_url != "", "pdf_url must be set after successful generation"
    assert invoice.tenant_id == tenant.id


# ---------------------------------------------------------------------------
# Tax calculation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_invoice_taxes_calculation(paid_order_factory, settings, tmp_path):
    """taxes = total * 0.15, rounded HALF_UP to 2 decimal places."""
    settings.MEDIA_ROOT = str(tmp_path)

    # 33.33 * 0.15 = 4.9995 → rounds to 5.00
    order = paid_order_factory(total=Decimal("33.33"))
    invoice = InvoiceService().generate_invoice_for_order(order.id)

    expected_taxes = (Decimal("33.33") * Decimal("0.15")).quantize(
        Decimal("0.01"), rounding="ROUND_HALF_UP"
    )
    assert invoice.taxes == expected_taxes


# ---------------------------------------------------------------------------
# PDF file creation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_invoice_pdf_file_exists(paid_order_factory, settings, tmp_path):
    """The generated PDF file must exist on disk under MEDIA_ROOT/invoices/."""
    settings.MEDIA_ROOT = str(tmp_path)

    order = paid_order_factory(total=Decimal("75.00"))
    invoice = InvoiceService().generate_invoice_for_order(order.id)

    media_url = getattr(settings, "MEDIA_URL", "/media/")
    pdf_relative = invoice.pdf_url.removeprefix(media_url)
    pdf_path = Path(settings.MEDIA_ROOT) / pdf_relative
    assert pdf_path.exists(), f"Expected PDF at {pdf_path}, not found"
    assert pdf_path.stat().st_size > 0, "PDF file must not be empty"


# ---------------------------------------------------------------------------
# Two-phase generation: DB row committed before PDF
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_invoice_db_row_committed_before_pdf(paid_order_factory, settings, tmp_path):
    """The Invoice row is committed to the DB (pdf_url='') before PDF rendering.

    This verifies the two-phase split: DB lock released before any file I/O.
    We simulate this by patching render_invoice_pdf to capture the invoice id
    and verify the row is already in the DB at that point.
    """
    settings.MEDIA_ROOT = str(tmp_path)
    from apps.invoice import pdf as pdf_module
    from apps.invoice.models import Invoice

    seen_in_db: list[bool] = []
    original_render = pdf_module.render_invoice_pdf

    def _capturing_render(**kwargs):
        invoice_id = kwargs["invoice_id"]
        exists = Invoice.objects.filter(id=invoice_id).exists()
        seen_in_db.append(exists)
        return original_render(**kwargs)

    order = paid_order_factory(total=Decimal("60.00"))
    with patch.object(pdf_module, "render_invoice_pdf", side_effect=_capturing_render):
        InvoiceService().generate_invoice_for_order(order.id)

    assert (
        seen_in_db == [True]
    ), "Invoice row must already be committed in the DB when render_invoice_pdf is called"


# ---------------------------------------------------------------------------
# PDF failure — row survives with pdf_url=""
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_invoice_pdf_failure_leaves_row_intact(paid_order_factory, settings, tmp_path):
    """If PDF rendering raises, the Invoice row is retained with pdf_url=''.

    The sequence number must not be re-allocated on the failed attempt.
    """
    settings.MEDIA_ROOT = str(tmp_path)
    from apps.invoice.models import Invoice, InvoiceSequence

    order = paid_order_factory(total=Decimal("45.00"))

    with patch("apps.invoice.pdf.render_invoice_pdf", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            InvoiceService().generate_invoice_for_order(order.id)

    # The Invoice row must exist.
    invoice = Invoice.objects.get(order_id=order.id)
    assert invoice.pdf_url == "", "pdf_url must be empty after a failed PDF render"
    assert invoice.number == 1, "invoice number must have been allocated"

    # Sequence advanced exactly once (the failed PDF did not re-allocate).
    seq = InvoiceSequence.objects.get(tenant=order.tenant)
    assert seq.last_number == 1


# ---------------------------------------------------------------------------
# PDF retry — after failure, next call renders PDF and updates pdf_url
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_invoice_pdf_retry_succeeds(paid_order_factory, settings, tmp_path):
    """After a PDF failure (pdf_url=''), the next call renders the PDF.

    No new sequence number is allocated; the existing Invoice row is updated.
    """
    settings.MEDIA_ROOT = str(tmp_path)
    from apps.invoice.models import Invoice, InvoiceSequence

    order = paid_order_factory(total=Decimal("55.00"))

    # First call: PDF fails.
    with patch("apps.invoice.pdf.render_invoice_pdf", side_effect=OSError("transient")):
        with pytest.raises(OSError):
            InvoiceService().generate_invoice_for_order(order.id)

    invoice_after_failure = Invoice.objects.get(order_id=order.id)
    assert invoice_after_failure.pdf_url == ""

    # Second call: PDF succeeds.
    invoice_after_retry = InvoiceService().generate_invoice_for_order(order.id)

    assert (
        invoice_after_retry.id == invoice_after_failure.id
    ), "Retry must return the same Invoice row, not create a new one"
    assert (
        invoice_after_retry.pdf_url != ""
    ), "pdf_url must be set after successful retry"
    assert (
        invoice_after_retry.number == invoice_after_failure.number
    ), "Sequence number must not change on retry"

    # Sequence must still be 1 — not incremented on the retry.
    seq = InvoiceSequence.objects.get(tenant=order.tenant)
    assert seq.last_number == 1


@pytest.mark.django_db(transaction=True)
def test_invoice_pdf_retry_does_not_rerender_if_already_set(
    paid_order_factory, settings, tmp_path
):
    """After full success (pdf_url set), a second call skips render_invoice_pdf."""
    settings.MEDIA_ROOT = str(tmp_path)
    from apps.invoice import pdf as pdf_module

    order = paid_order_factory(total=Decimal("30.00"))
    svc = InvoiceService()

    # First call: full success.
    invoice_first = svc.generate_invoice_for_order(order.id)
    assert invoice_first.pdf_url != ""

    render_call_count = 0

    def _counting_render(**kwargs):
        nonlocal render_call_count
        render_call_count += 1
        return "/media/invoices/fake.pdf"

    # Second call: should be a fast-path return, render not called.
    with patch.object(pdf_module, "render_invoice_pdf", side_effect=_counting_render):
        invoice_second = svc.generate_invoice_for_order(order.id)

    assert (
        render_call_count == 0
    ), "render_invoice_pdf must not be called when pdf_url is already set"
    assert invoice_second.id == invoice_first.id


# ---------------------------------------------------------------------------
# Idempotency — double call (both fully successful)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_invoice_idempotent_double_call(paid_order_factory, settings, tmp_path):
    """Calling generate_invoice_for_order twice returns the same Invoice."""
    settings.MEDIA_ROOT = str(tmp_path)

    order = paid_order_factory(total=Decimal("50.00"))
    svc = InvoiceService()
    invoice_a = svc.generate_invoice_for_order(order.id)
    invoice_b = svc.generate_invoice_for_order(order.id)

    assert invoice_a.id == invoice_b.id, "Both calls must return the same Invoice"
    assert invoice_a.number == invoice_b.number

    # Sequence counter must have advanced exactly once.
    from apps.invoice.models import InvoiceSequence

    seq = InvoiceSequence.objects.get(tenant=order.tenant)
    assert seq.last_number == 1


# ---------------------------------------------------------------------------
# Monotonic per-tenant numbering
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_invoice_monotonic_numbers_same_tenant(paid_order_factory, settings, tmp_path):
    """Three orders in the same tenant receive sequential invoice numbers."""
    settings.MEDIA_ROOT = str(tmp_path)

    svc = InvoiceService()
    inv1 = svc.generate_invoice_for_order(paid_order_factory().id)
    inv2 = svc.generate_invoice_for_order(paid_order_factory().id)
    inv3 = svc.generate_invoice_for_order(paid_order_factory().id)

    numbers = sorted([inv1.number, inv2.number, inv3.number])
    assert numbers == [1, 2, 3]


@pytest.mark.django_db(transaction=True)
def test_invoice_numbering_resets_per_tenant(
    paid_order_factory, other_tenant, settings, tmp_path
):
    """The first invoice for a different tenant starts at number 1."""
    from apps.addresses.models import Address
    from apps.cart.models import Cart
    from apps.order.models import Order
    from apps.payment.models import PaymentMethod
    from apps.tenant.context import tenant_context

    settings.MEDIA_ROOT = str(tmp_path)
    svc = InvoiceService()

    # Generate two invoices under the primary tenant (fixture-provided context).
    svc.generate_invoice_for_order(paid_order_factory().id)
    svc.generate_invoice_for_order(paid_order_factory().id)

    # Switch to the other tenant and generate one invoice there.
    with tenant_context(other_tenant):
        uid = uuid.uuid4()
        cart = Cart.objects.create(user_id=uid, currency="USD")
        addr = Address.objects.create(user_id=uid, country="US", city="X", details="Y")
        pm = PaymentMethod.objects.create(gateway_slug="dummy_success")
        other_order = Order.objects.create(
            user_id=uid,
            cart=cart,
            address=addr,
            payment_method=pm,
            subtotal=Decimal("10.00"),
            discount_amount=Decimal("0"),
            total=Decimal("10.00"),
            currency="USD",
            idempotency_key=uuid.uuid4(),
            status="PAID",
        )
        other_invoice = svc.generate_invoice_for_order(other_order.id)

    assert other_invoice.number == 1, "First invoice for a new tenant must start at 1"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_order_not_found_raises(tenant):
    """Passing a non-existent order_id raises OrderNotFound."""
    with pytest.raises(OrderNotFound):
        InvoiceService().generate_invoice_for_order(uuid.uuid4())


@pytest.mark.django_db(transaction=True)
def test_order_not_paid_raises(paid_order_factory, settings, tmp_path):
    """Passing an order that is NOT in PAID status raises OrderNotPaid."""
    settings.MEDIA_ROOT = str(tmp_path)
    from apps.order.models import Order

    order = paid_order_factory(total=Decimal("20.00"))
    Order.objects.filter(pk=order.pk).update(status="PENDING_PAYMENT")
    order.refresh_from_db()

    with pytest.raises(OrderNotPaid):
        InvoiceService().generate_invoice_for_order(order.id)
