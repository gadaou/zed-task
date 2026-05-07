"""Tests for the ``process_payment(order_id)`` Celery task.

Coverage:
    test_process_payment_success
        — REQUIRES_CONFIRMATION payment is authorised; result contains
          order_id, payment_id, and status=AUTHORIZED.
    test_process_payment_idempotent_double_call
        — Two calls produce exactly one gateway call (mirrors
          test_celery_task_idempotent_retry in test_services.py).
    test_process_payment_order_not_found
        — Non-existent order_id returns {"status": "not_found"} without raising
          (so Celery does not treat it as retriable).
    test_process_payment_no_payment_for_order
        — Order exists but has no linked Payment; returns
          {"status": "no_payment"} without raising.
    test_process_payment_declined
        — DummyFailingGateway → gateway declines; returns {"status": "FAILED"}
          without raising (not retriable).
    test_process_payment_timeout_propagates_for_retry
        — DummyTimeoutGateway raises GatewayTimeout so Celery's autoretry_for
          can reschedule the task.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.payment.exceptions import GatewayTimeout
from apps.payment.gateways.dummy import DummySuccessGateway
from apps.payment.models import Payment


# ---------------------------------------------------------------------------
# Helpers (shared with test_services.py pattern)
# ---------------------------------------------------------------------------


def _build_order_for_payment(payment, provider_override: str | None = None):
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
    pm = PaymentMethod.objects.create(
        gateway_slug=provider_override or payment.provider,
    )
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
# Success
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_process_payment_success(payment_factory, tenant):
    """Happy path: payment is authorised and result contains all keys."""
    from apps.payment.tasks import process_payment

    payment = payment_factory(provider="dummy_success")
    order = _build_order_for_payment(payment)

    with patch("apps.invoice.tasks.generate_invoice.apply_async"):
        result = process_payment(str(order.id))

    assert result["status"] == Payment.Status.AUTHORIZED
    assert result["payment_id"] == str(payment.id)
    assert result["order_id"] == str(order.id)

    payment.refresh_from_db()
    assert payment.status == Payment.Status.AUTHORIZED


# ---------------------------------------------------------------------------
# Idempotency — double call
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_process_payment_idempotent_double_call(payment_factory, tenant):
    """Two calls produce exactly one gateway call."""
    from apps.payment.tasks import process_payment

    payment = payment_factory(provider="dummy_success")
    order = _build_order_for_payment(payment)

    call_count = 0
    original_authorize = DummySuccessGateway.authorize_payment

    def _counting_authorize(self_gw, order_, pm, metadata=None):
        nonlocal call_count
        call_count += 1
        return original_authorize(self_gw, order_, pm, metadata)

    with patch.object(DummySuccessGateway, "authorize_payment", _counting_authorize):
        process_payment(str(order.id))
        process_payment(str(order.id))  # second call — idempotent

    assert (
        call_count == 1
    ), "Gateway must be called exactly once; second call is a no-op"

    payment.refresh_from_db()
    assert payment.status == Payment.Status.AUTHORIZED


# ---------------------------------------------------------------------------
# Order not found
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_process_payment_order_not_found(tenant):
    """Non-existent order_id returns not_found without raising."""
    from apps.payment.tasks import process_payment

    missing_id = str(uuid.uuid4())
    result = process_payment(missing_id)

    assert result["status"] == "not_found"
    assert result["order_id"] == missing_id


# ---------------------------------------------------------------------------
# No payment linked to the order
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_process_payment_no_payment_for_order(payment_factory, tenant):
    """Order exists but has no Payment; returns no_payment without raising."""
    import uuid as _uuid

    from apps.addresses.models import Address
    from apps.cart.models import Cart
    from apps.order.models import Order
    from apps.payment.models import PaymentMethod
    from apps.payment.tasks import process_payment

    # Build an order whose cart has NO payment row.
    user_id = _uuid.uuid4()
    cart = Cart.objects.create(user_id=user_id, currency="USD")
    address = Address.objects.create(
        user_id=user_id,
        country="US",
        city="Test City",
        details="1 Test St",
    )
    pm = PaymentMethod.objects.create(gateway_slug="dummy_success")
    order = Order.objects.create(
        user_id=user_id,
        cart=cart,
        address=address,
        payment_method=pm,
        subtotal=Decimal("10.00"),
        discount_amount=Decimal("0"),
        total=Decimal("10.00"),
        currency="USD",
        idempotency_key=_uuid.uuid4(),
    )

    result = process_payment(str(order.id))

    assert result["status"] == "no_payment"
    assert result["order_id"] == str(order.id)


# ---------------------------------------------------------------------------
# Gateway declined
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_process_payment_declined(payment_factory, tenant):
    """DummyFailingGateway → task returns FAILED without raising."""
    from apps.payment.tasks import process_payment

    payment = payment_factory(provider="dummy_failing")
    order = _build_order_for_payment(payment)

    result = process_payment(str(order.id))

    assert result["status"] == "FAILED"
    assert result["payment_id"] == str(payment.id)

    payment.refresh_from_db()
    assert payment.status == Payment.Status.FAILED


# ---------------------------------------------------------------------------
# Gateway timeout — must propagate for autoretry_for
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_process_payment_timeout_propagates_for_retry(payment_factory, tenant):
    """DummyTimeoutGateway raises GatewayTimeout so autoretry_for can trigger."""
    from apps.payment.tasks import process_payment

    payment = payment_factory(provider="dummy_timeout")
    order = _build_order_for_payment(payment)

    with pytest.raises(GatewayTimeout):
        process_payment(str(order.id))

    # Payment must remain in REQUIRES_CONFIRMATION so the next retry can attempt it.
    payment.refresh_from_db()
    assert payment.status == Payment.Status.REQUIRES_CONFIRMATION
