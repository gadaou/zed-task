"""Shared pytest fixtures for the cart test suite.

Every fixture that touches a ``TenantAwareModel`` must run inside an active
``tenant_context``; otherwise the ``TenantAwareManager`` raises
``TenantContextMissing`` (PROJECT_SPEC §4.2). The ``tenant`` fixture below
both creates a Tenant row and activates it for the test, so any fixture that
depends on ``tenant`` (and any test that depends on those fixtures) runs
inside the right context automatically.

ADR-NOTE: PROJECT_SPEC §6.5 calls for ``factory_boy`` factories. We've
deliberately deferred that dependency until there are enough models to
justify it — the explicit builder fixtures here serve the same role for now
and keep the test setup obvious to a first-time reader.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Callable

import pytest

from apps.cart.models import Cart
from apps.catalog.models import Product
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant


# ---------------------------------------------------------------------------
# Gateway registry guard
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def ensure_dummy_gateways_registered():
    """Ensure the dummy payment gateways are in the registry for every cart test.

    The ``register_dummies`` fixture in ``apps/payment/tests/conftest.py``
    has ``autouse=True`` within its own package and **unregisters** the dummy
    slugs after each test there.  When payment tests run before cart tests in
    the same session the registry ends up empty, causing cart view tests that
    exercise the payment-method flow to fail with ``UnsupportedGateway``.

    This fixture re-registers any missing dummy slugs before each cart test
    and does not touch any slug that is already registered (safe to combine
    with the payment package's own fixture in mixed-module test runs).
    """
    from apps.payment.gateways.dummy import (
        DummyFailingGateway,
        DummyGateway,
        DummySuccessGateway,
        DummyTimeoutGateway,
    )
    from apps.payment.gateways.registry import _REGISTRY, register_payment_gateway

    _to_register = [
        (DummySuccessGateway.slug, DummySuccessGateway),
        (DummyFailingGateway.slug, DummyFailingGateway),
        (DummyTimeoutGateway.slug, DummyTimeoutGateway),
        (DummyGateway.slug, DummyGateway),
        ("mock", DummySuccessGateway),
    ]
    _registered_now: list[str] = []
    for slug, cls in _to_register:
        if slug not in _REGISTRY:
            register_payment_gateway(slug, cls)
            _registered_now.append(slug)

    yield

    # Only remove slugs that *this* fixture added — do not remove slugs that
    # were already registered when this fixture ran (they belong to another
    # lifecycle).
    from apps.payment.gateways.registry import unregister_payment_gateway

    for slug in _registered_now:
        if slug in _REGISTRY:
            unregister_payment_gateway(slug)


@pytest.fixture
def tenant(db) -> Tenant:
    """Create a Tenant and activate it for the duration of the test.

    The ``db`` fixture is required so pytest-django sets up the test database
    transaction before this fixture runs. Yielding inside ``tenant_context``
    means every assertion in the test body sees the same active tenant; the
    context is automatically reset on teardown even if the test raises.
    """
    t = Tenant.objects.create(
        name="Test Tenant",
        domain=f"tenant-{uuid.uuid4().hex[:12]}.test",
    )
    with tenant_context(t):
        yield t


@pytest.fixture
def product_factory(tenant: Tenant) -> Callable[..., Product]:
    """Builder for Product rows scoped to the active tenant.

    Returns a callable so a single test can create multiple distinct products
    without redefining the fixture. Tenant is auto-stamped by the
    ``TenantAwareManager`` from the active context.
    """

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
    """Builder for Cart rows scoped to the active tenant.

    Defaults: a fresh ``user_id`` UUID per call, ACTIVE status, USD currency.
    Override any of these by passing kwargs to the returned callable.
    """

    def _build(
        user_id: uuid.UUID | None = None,
        currency: str = "USD",
    ) -> Cart:
        return Cart.objects.create(
            user_id=user_id or uuid.uuid4(),
            currency=currency,
        )

    return _build
