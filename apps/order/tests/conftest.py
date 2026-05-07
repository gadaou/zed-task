"""Shared pytest fixtures for the order / checkout test suite.

Every fixture that touches a ``TenantAwareModel`` runs inside an active
``tenant_context`` (provided by the ``tenant`` fixture). Any fixture that
depends on ``tenant`` is therefore automatically scoped to the right tenant.

The ``redis_client`` fixture provides a ``fakeredis.FakeRedis`` instance and
monkeypatches ``apps.core.redis.get_redis_client`` so all service code uses
the in-memory fake without touching a real Redis server.

The ``checkout_service`` fixture wires all collaborators (fakeredis,
dispatched-call recorder) so tests can assert on side-effects without
worrying about real infrastructure.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Callable

import pytest

from apps.addresses.models import Address
from apps.cart.models import Cart
from apps.cart.services import add_product_to_cart
from apps.catalog.models import Product
from apps.payment.models import PaymentMethod
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant(db) -> Tenant:
    """Create a Tenant and activate it for the duration of the test."""
    t = Tenant.objects.create(
        name="Test Tenant",
        domain=f"tenant-{uuid.uuid4().hex[:12]}.test",
    )
    with tenant_context(t):
        yield t


@pytest.fixture
def other_tenant(db) -> Tenant:
    """A second tenant for cross-tenant isolation tests.

    Does NOT activate the context — tests must wrap their own usage.
    """
    return Tenant.objects.create(
        name="Other Tenant",
        domain=f"tenant-{uuid.uuid4().hex[:12]}.test",
    )


# ---------------------------------------------------------------------------
# Fakeredis
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis():
    """Return a ``fakeredis.FakeRedis`` instance with ``decode_responses=True``."""
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=False)
def redis_client(fake_redis, monkeypatch):
    """Monkeypatch ``apps.core.redis.get_redis_client`` to return the fake.

    ``autouse=False`` — tests that do not declare this fixture in their
    signature keep the real (or unpatched) implementation.  Checkout tests
    should always declare it to avoid hitting a real Redis server.
    """
    monkeypatch.setattr("apps.core.redis.get_redis_client", lambda: fake_redis)
    return fake_redis


# ---------------------------------------------------------------------------
# Domain model builders
# ---------------------------------------------------------------------------


@pytest.fixture
def product_factory(tenant: Tenant) -> Callable[..., Product]:
    """Build Product rows scoped to the active tenant."""

    def _build(
        name: str | None = None,
        price: Decimal | str = Decimal("10.00"),
        currency: str = "USD",
        stock: int = 100,
    ) -> Product:
        return Product.objects.create(
            name=name or f"product-{uuid.uuid4().hex[:8]}",
            price=Decimal(price),
            currency=currency,
            stock=stock,
        )

    return _build


@pytest.fixture
def cart_factory(tenant: Tenant) -> Callable[..., Cart]:
    """Build Cart rows scoped to the active tenant."""

    def _build(
        user_id: uuid.UUID | None = None,
        currency: str = "USD",
    ) -> Cart:
        return Cart.objects.create(
            user_id=user_id or uuid.uuid4(),
            currency=currency,
        )

    return _build


@pytest.fixture
def address_factory(tenant: Tenant) -> Callable[..., Address]:
    """Build Address rows scoped to the active tenant."""

    def _build(
        user_id: uuid.UUID,
        country: str = "US",
        city: str = "Springfield",
        details: str = "1 Main St",
        is_default: bool = True,
    ) -> Address:
        return Address.objects.create(
            user_id=user_id,
            country=country,
            city=city,
            details=details,
            is_default=is_default,
        )

    return _build


@pytest.fixture
def payment_method_factory(tenant: Tenant) -> Callable[..., PaymentMethod]:
    """Build PaymentMethod rows scoped to the active tenant."""

    def _build(gateway_slug: str = "dummy_success") -> PaymentMethod:
        return PaymentMethod.objects.create(gateway_slug=gateway_slug)

    return _build


# ---------------------------------------------------------------------------
# Checkout service with injected fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatched_payments() -> list:
    """Collects payment_ids passed to the payment dispatcher."""
    return []


@pytest.fixture
def checkout_service(fake_redis, dispatched_payments):
    """CheckoutService wired with fakeredis and a recording dispatcher."""
    from apps.core.idempotency import IdempotencyManager
    from apps.order.services import CheckoutService

    def _record_dispatch(payment_id):
        dispatched_payments.append(payment_id)

    idem_manager = IdempotencyManager(
        redis_client=fake_redis,
        inprogress_ttl_s=60,
    )
    return CheckoutService(
        redis_client=fake_redis,
        payment_dispatcher=_record_dispatch,
        idem_manager=idem_manager,
        lock_ttl_ms=5000,
    )


# ---------------------------------------------------------------------------
# Convenience: a fully-loaded cart ready for checkout
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded_cart(cart_factory, product_factory, address_factory, payment_method_factory):
    """Return (cart, address, payment_method) with one item in the cart."""
    cart = cart_factory()
    product = product_factory(price=Decimal("25.00"), stock=10)
    add_product_to_cart(cart, product, quantity=2)
    cart.refresh_from_db()

    address = address_factory(user_id=cart.user_id)
    pm = payment_method_factory()
    return cart, address, pm
