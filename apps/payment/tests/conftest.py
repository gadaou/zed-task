"""Shared pytest fixtures for the payment test suite.

Fixtures
--------
``tenant``
    Creates a ``Tenant`` and activates it for the duration of the test.

``payment_factory``
    Creates ``Payment`` rows in ``REQUIRES_CONFIRMATION`` state, scoped to the
    active tenant.  Accepts an optional ``provider`` argument to pin a gateway
    slug.

``register_dummies``
    ``autouse=True`` fixture that ensures the three dummy gateways are
    registered before each test and removed afterwards.  This makes tests
    hermetic — a test that registers additional gateways (or double-registers)
    does not pollute subsequent tests.

    Because ``PaymentConfig.ready()`` registers the dummies once at app startup
    and ``register_payment_gateway`` raises on duplicate registration, this
    fixture works by:
      1. Unregistering all dummies before the test.
      2. Re-registering them fresh.
      3. Unregistering them after (so the next fixture run starts clean).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

import pytest

if TYPE_CHECKING:
    from apps.payment.models import Payment

from apps.payment.gateways.dummy import (
    DummyFailingGateway,
    DummyGateway,
    DummySuccessGateway,
    DummyTimeoutGateway,
)
from apps.payment.gateways.registry import (
    _REGISTRY,
    register_payment_gateway,
    unregister_payment_gateway,
)
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant

# Slugs managed by this fixture
_DUMMY_SLUGS = [
    DummySuccessGateway.slug,
    DummyFailingGateway.slug,
    DummyTimeoutGateway.slug,
    DummyGateway.slug,
    "mock",
]


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def register_dummies():
    """Ensure a clean, predictable registry for every payment test.

    Strategy:
    - Remove any pre-registered dummy slugs (they may already be registered
      from ``PaymentConfig.ready()`` or a previous test).
    - Register them fresh.
    - Yield to the test.
    - Remove them again so the next test starts from the same baseline.

    Any slug the test registers itself (beyond the four dummies) is cleaned up
    by the test fixture itself — this fixture only manages the four core slugs.
    """
    # Teardown of previous state
    for slug in _DUMMY_SLUGS:
        if slug in _REGISTRY:
            unregister_payment_gateway(slug)

    # Fresh registration
    register_payment_gateway(DummySuccessGateway.slug, DummySuccessGateway)
    register_payment_gateway(DummyFailingGateway.slug, DummyFailingGateway)
    register_payment_gateway(DummyTimeoutGateway.slug, DummyTimeoutGateway)
    register_payment_gateway(DummyGateway.slug, DummyGateway)
    register_payment_gateway("mock", DummySuccessGateway)

    yield

    # Post-test cleanup
    for slug in _DUMMY_SLUGS:
        if slug in _REGISTRY:
            unregister_payment_gateway(slug)


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant(db) -> Tenant:
    """Create a Tenant and activate it for the duration of the test."""
    t = Tenant.objects.create(
        name="Payment Test Tenant",
        domain=f"pay-{uuid.uuid4().hex[:12]}.test",
    )
    with tenant_context(t):
        yield t


# ---------------------------------------------------------------------------
# Domain model builder
# ---------------------------------------------------------------------------


@pytest.fixture
def payment_factory(tenant: Tenant) -> Callable[..., "Payment"]:  # type: ignore[name-defined]
    """Build ``Payment`` rows in ``REQUIRES_CONFIRMATION``, scoped to the active tenant."""
    from apps.cart.models import Cart
    from apps.payment.models import Payment

    def _build(
        provider: str = "dummy_success",
        amount: Decimal | str = Decimal("50.00"),
        currency: str = "USD",
    ) -> Payment:
        cart = Cart.objects.create(
            user_id=uuid.uuid4(),
            currency=currency,
        )
        return Payment.objects.create(
            cart=cart,
            provider=provider,
            amount=Decimal(amount),
            currency=currency,
        )

    return _build
