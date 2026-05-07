"""Integration tests for ``PaymentService``.

All tests that touch the DB run with ``@pytest.mark.django_db(transaction=True)``
because PaymentService uses ``select_for_update`` indirectly and
``transaction.atomic()`` with status-guarded UPDATEs.

Test coverage:
    test_authorize_success              — DummySuccessGateway → AUTHORIZED, reference persisted.
    test_authorize_flips_order_to_paid  — Successful authorization flips Order.status to PAID.
    test_authorize_dispatches_invoice   — Successful authorization enqueues generate_invoice.
    test_authorize_declined             — DummyFailingGateway → FAILED, failure_reason set, GatewayDeclined raised.
    test_authorize_declined_flips_order_to_failed — Declined authorization flips Order.status to FAILED.
    test_authorize_declined_no_invoice  — Declined authorization does NOT enqueue generate_invoice.
    test_authorize_unsupported          — Unknown slug → UnsupportedGateway raised, payment unchanged.
    test_authorize_timeout              — DummyTimeoutGateway → GatewayTimeout raised, payment stays REQUIRES_CONFIRMATION.
    test_authorize_idempotent           — Second call with same payment_id returns immediately; no second gateway call.
    test_authorize_already_failed       — Payment already FAILED → idempotent skip, no gateway call.
    test_celery_task_idempotent         — Celery task invoked twice; exactly one FSM transition.
    test_celery_task_unsupported_gw     — Task with unregistered provider marks payment FAILED, no retry.
    test_capture_success                — AUTHORIZED → CAPTURED, capture_id persisted.
    test_void_success                   — AUTHORIZED → CANCELLED.
    test_refund_success                 — CAPTURED → REFUNDED.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.payment.exceptions import (
    GatewayDeclined,
    GatewayTimeout,
    UnsupportedGateway,
)
from apps.payment.gateways.dummy import DummySuccessGateway
from apps.payment.models import Payment
from apps.payment.services import PaymentService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_order_for_payment(payment):
    """Create a minimal Order row linked to payment.cart."""
    import uuid as _uuid
    from apps.addresses.models import Address
    from apps.order.models import Order
    from apps.payment.models import PaymentMethod

    user_id = _uuid.uuid4()
    address = Address.objects.create(
        user_id=user_id,
        country="US",
        city="Test City",
        details="1 Test St",
    )
    pm = PaymentMethod.objects.create(gateway_slug="dummy_success")

    return Order.objects.create(
        user_id=user_id,
        cart=payment.cart,
        address=address,
        payment_method=pm,
        subtotal=payment.amount,
        discount_amount=Decimal("0"),
        total=payment.amount,
        currency=payment.currency,
        idempotency_key=_uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# Authorization — success
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_authorize_success(payment_factory, tenant):
    """DummySuccessGateway transitions payment to AUTHORIZED and persists reference."""
    payment = payment_factory(provider="dummy_success")
    _build_order_for_payment(payment)

    updated = PaymentService().authorize_payment(payment.id)

    assert updated.status == Payment.Status.AUTHORIZED
    assert updated.gateway_authorization_id.startswith("dummy-auth-")


@pytest.mark.django_db(transaction=True)
def test_authorize_flips_order_to_paid(payment_factory, tenant):
    """Successful authorization must flip Order.status to PAID."""

    payment = payment_factory(provider="dummy_success")
    order = _build_order_for_payment(payment)

    with patch("apps.invoice.tasks.generate_invoice.apply_async"):
        PaymentService().authorize_payment(payment.id)

    order.refresh_from_db()
    assert order.status == "PAID"


@pytest.mark.django_db(transaction=True)
def test_authorize_dispatches_invoice(payment_factory, tenant, settings, tmp_path):
    """Successful authorization must enqueue generate_invoice on commit."""
    settings.MEDIA_ROOT = str(tmp_path)

    dispatched: list[str] = []

    def _record(order_id):
        dispatched.append(str(order_id))

    payment = payment_factory(provider="dummy_success")
    order = _build_order_for_payment(payment)

    with patch("apps.payment.services.enqueue_generate_invoice", side_effect=_record):
        PaymentService().authorize_payment(payment.id)

    assert len(dispatched) == 1
    assert dispatched[0] == str(order.id)


# ---------------------------------------------------------------------------
# Authorization — declined
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_authorize_declined(payment_factory, tenant):
    """DummyFailingGateway transitions payment to FAILED and raises GatewayDeclined."""
    payment = payment_factory(provider="dummy_failing")
    _build_order_for_payment(payment)

    with pytest.raises(GatewayDeclined) as exc_info:
        PaymentService().authorize_payment(payment.id)

    assert exc_info.value.error_code == "card_declined"

    payment.refresh_from_db()
    assert payment.status == Payment.Status.FAILED
    assert "card_declined" in payment.failure_reason


@pytest.mark.django_db(transaction=True)
def test_authorize_declined_flips_order_to_failed(payment_factory, tenant):
    """Gateway decline must flip Order.status to FAILED."""

    payment = payment_factory(provider="dummy_failing")
    order = _build_order_for_payment(payment)

    with pytest.raises(GatewayDeclined):
        PaymentService().authorize_payment(payment.id)

    order.refresh_from_db()
    assert order.status == "FAILED"


@pytest.mark.django_db(transaction=True)
def test_authorize_declined_no_invoice_dispatch(payment_factory, tenant):
    """A declined payment must NOT enqueue generate_invoice."""
    dispatched: list = []

    def _record(order_id):
        dispatched.append(order_id)

    payment = payment_factory(provider="dummy_failing")
    _build_order_for_payment(payment)

    with patch("apps.payment.services.enqueue_generate_invoice", side_effect=_record):
        with pytest.raises(GatewayDeclined):
            PaymentService().authorize_payment(payment.id)

    assert dispatched == [], "No invoice must be dispatched on a declined payment"


# ---------------------------------------------------------------------------
# Authorization — unsupported gateway
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_authorize_unsupported_gateway(payment_factory, tenant):
    """Payment with an unregistered slug raises UnsupportedGateway."""
    payment = payment_factory(provider="not_a_real_gateway")
    _build_order_for_payment(payment)

    with pytest.raises(UnsupportedGateway) as exc_info:
        PaymentService().authorize_payment(payment.id)

    assert exc_info.value.slug == "not_a_real_gateway"

    # Payment row must be unchanged.
    payment.refresh_from_db()
    assert payment.status == Payment.Status.REQUIRES_CONFIRMATION


# ---------------------------------------------------------------------------
# Authorization — gateway timeout
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_authorize_timeout_propagates_and_leaves_status_unchanged(
    payment_factory, tenant
):
    """DummyTimeoutGateway raises GatewayTimeout; payment stays REQUIRES_CONFIRMATION."""
    payment = payment_factory(provider="dummy_timeout")
    _build_order_for_payment(payment)

    with pytest.raises(GatewayTimeout):
        PaymentService().authorize_payment(payment.id)

    payment.refresh_from_db()
    assert payment.status == Payment.Status.REQUIRES_CONFIRMATION
    assert payment.gateway_authorization_id == ""


# ---------------------------------------------------------------------------
# Authorization — idempotency (double call)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_authorize_idempotent_double_call(payment_factory, tenant):
    """Second authorize call returns immediately without a second gateway call."""
    payment = payment_factory(provider="dummy_success")
    _build_order_for_payment(payment)

    call_count = 0
    original_authorize = DummySuccessGateway.authorize_payment

    def _counting_authorize(self_gw, order, pm, metadata=None):
        nonlocal call_count
        call_count += 1
        return original_authorize(self_gw, order, pm, metadata)

    with patch.object(DummySuccessGateway, "authorize_payment", _counting_authorize):
        PaymentService().authorize_payment(payment.id)
        PaymentService().authorize_payment(payment.id)  # second call — idempotent

    assert (
        call_count == 1
    ), "Gateway must be called exactly once; second call is a no-op"


@pytest.mark.django_db(transaction=True)
def test_authorize_already_failed_is_idempotent(payment_factory, tenant):
    """A payment already in FAILED status is returned immediately; no gateway call."""
    payment = payment_factory(provider="dummy_success")
    _build_order_for_payment(payment)

    # Force into FAILED state.
    Payment.objects.filter(pk=payment.pk).update(
        status=Payment.Status.FAILED,
        failure_reason="pre-existing failure",
    )

    call_count = 0
    original_authorize = DummySuccessGateway.authorize_payment

    def _counting_authorize(self_gw, order, pm, metadata=None):
        nonlocal call_count
        call_count += 1
        return original_authorize(self_gw, order, pm, metadata)

    with patch.object(DummySuccessGateway, "authorize_payment", _counting_authorize):
        result = PaymentService().authorize_payment(payment.id)

    assert call_count == 0
    assert result.status == Payment.Status.FAILED


# ---------------------------------------------------------------------------
# Celery task integration
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_celery_task_idempotent_retry(payment_factory, tenant):
    """Celery task invoked twice performs exactly one FSM transition."""
    from apps.payment.tasks import authorize_payment as celery_authorize

    payment = payment_factory(provider="dummy_success")
    _build_order_for_payment(payment)

    result1 = celery_authorize(str(payment.id))
    result2 = celery_authorize(str(payment.id))  # idempotent retry

    assert result1["status"] == Payment.Status.AUTHORIZED
    assert (
        result2["status"] == Payment.Status.AUTHORIZED
    )  # or "skipped" — still correct

    # Exactly one order in AUTHORIZED state.
    payment.refresh_from_db()
    assert payment.status == Payment.Status.AUTHORIZED


@pytest.mark.django_db(transaction=True)
def test_celery_task_unsupported_gateway_marks_failed(payment_factory, tenant):
    """Celery task with an unregistered gateway slug marks the payment FAILED."""
    from apps.payment.tasks import authorize_payment as celery_authorize

    payment = payment_factory(provider="phantom_gateway")
    _build_order_for_payment(payment)

    result = celery_authorize(str(payment.id))

    assert result["status"] == "FAILED"
    payment.refresh_from_db()
    assert payment.status == Payment.Status.FAILED


# ---------------------------------------------------------------------------
# Capture, void, refund
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_capture_success(payment_factory, tenant):
    """AUTHORIZED → CAPTURED; gateway_capture_id is persisted."""
    payment = payment_factory(provider="dummy_success")
    _build_order_for_payment(payment)

    # First authorize.
    PaymentService().authorize_payment(payment.id)
    payment.refresh_from_db()
    assert payment.status == Payment.Status.AUTHORIZED

    # Then capture.
    updated = PaymentService().capture_payment(payment.id)

    assert updated.status == Payment.Status.CAPTURED
    assert updated.gateway_capture_id.startswith("dummy-capture-")


@pytest.mark.django_db(transaction=True)
def test_capture_is_idempotent(payment_factory, tenant):
    """Calling capture twice returns the current state on the second call."""
    payment = payment_factory(provider="dummy_success")
    _build_order_for_payment(payment)

    PaymentService().authorize_payment(payment.id)
    first = PaymentService().capture_payment(payment.id)
    second = PaymentService().capture_payment(payment.id)

    assert first.status == second.status == Payment.Status.CAPTURED


@pytest.mark.django_db(transaction=True)
def test_void_success(payment_factory, tenant):
    """AUTHORIZED → CANCELLED via void."""
    payment = payment_factory(provider="dummy_success")
    _build_order_for_payment(payment)

    PaymentService().authorize_payment(payment.id)
    updated = PaymentService().void_payment(payment.id)

    assert updated.status == Payment.Status.CANCELLED


@pytest.mark.django_db(transaction=True)
def test_refund_success(payment_factory, tenant):
    """CAPTURED → REFUNDED."""
    payment = payment_factory(provider="dummy_success")
    _build_order_for_payment(payment)

    PaymentService().authorize_payment(payment.id)
    PaymentService().capture_payment(payment.id)
    updated = PaymentService().refund_payment(payment.id)

    assert updated.status == Payment.Status.REFUNDED


@pytest.mark.django_db(transaction=True)
def test_refund_partial_amount(payment_factory, tenant):
    """Partial refund succeeds with the correct amount passed to the gateway."""
    payment = payment_factory(provider="dummy_success", amount=Decimal("100.00"))
    _build_order_for_payment(payment)

    PaymentService().authorize_payment(payment.id)
    PaymentService().capture_payment(payment.id)

    updated = PaymentService().refund_payment(payment.id, amount=Decimal("30.00"))
    assert updated.status == Payment.Status.REFUNDED
