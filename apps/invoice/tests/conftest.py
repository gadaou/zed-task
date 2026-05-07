"""Shared pytest fixtures for the invoice test suite."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Callable

import pytest

from apps.addresses.models import Address
from apps.cart.models import Cart
from apps.order.models import Order
from apps.payment.models import PaymentMethod
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant


@pytest.fixture
def tenant(db) -> Tenant:
    """Create a Tenant and activate it for the duration of the test."""
    t = Tenant.objects.create(
        name="Invoice Test Tenant",
        domain=f"invoice-{uuid.uuid4().hex[:12]}.test",
    )
    with tenant_context(t):
        yield t


@pytest.fixture
def other_tenant(db) -> Tenant:
    """A second tenant — does NOT activate the context."""
    return Tenant.objects.create(
        name="Other Invoice Tenant",
        domain=f"invoice-other-{uuid.uuid4().hex[:12]}.test",
    )


@pytest.fixture
def paid_order_factory(tenant: Tenant) -> Callable[..., Order]:
    """Return a factory that builds Order rows in PAID status."""

    def _build(
        total: Decimal | str = Decimal("50.00"),
        currency: str = "USD",
    ) -> Order:
        user_id = uuid.uuid4()
        cart = Cart.objects.create(user_id=user_id, currency=currency)
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
            subtotal=Decimal(total),
            discount_amount=Decimal("0"),
            total=Decimal(total),
            currency=currency,
            idempotency_key=uuid.uuid4(),
            status="PAID",
        )
        return order

    return _build
