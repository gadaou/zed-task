"""Shared pytest fixtures for the coupon test suite.

Extends the pattern established by ``apps/cart/tests/conftest.py``:

* The ``tenant`` fixture creates and activates a Tenant for the test —
  every fixture below depends on it (transitively) so the
  ``TenantAwareManager`` always has a context (PROJECT_SPEC §4.2).
* Builder fixtures return *callables* so a test can produce multiple
  distinct rows without redefining the fixture.

ADR-NOTE: PROJECT_SPEC §6.5 mandates ``factory_boy``.  We continue the
existing repo convention of explicit builder fixtures while the model count
is small — once we cross ~10 models we will adopt factory_boy properly.

Note: validator unit tests do not need the Django DB and skip every fixture
that pulls one in.  Only the service-layer integration tests use the DB
fixtures here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any, Callable

import pytest

from apps.addresses.models import Address
from apps.cart.models import Cart
from apps.catalog.models import Product
from apps.coupon.models import Coupon
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant


@pytest.fixture
def tenant(db) -> Tenant:
    """Create a Tenant and activate it for the duration of the test.

    Mirrors the cart suite's fixture so the two suites compose if pytest ever
    pulls them into the same run.
    """
    t = Tenant.objects.create(
        name="Test Tenant",
        domain=f"tenant-{uuid.uuid4().hex[:12]}.test",
    )
    with tenant_context(t):
        yield t


@pytest.fixture
def other_tenant(db) -> Tenant:
    """A second tenant for cross-tenant isolation tests.

    Deliberately does NOT activate the context — tests that need to seed
    cross-tenant rows wrap their setup in their own ``tenant_context(...)``
    block to keep the active-tenant signal explicit.
    """
    return Tenant.objects.create(
        name="Other Tenant",
        domain=f"tenant-{uuid.uuid4().hex[:12]}.test",
    )


@pytest.fixture
def product_factory(tenant: Tenant) -> Callable[..., Product]:
    """Builder for Product rows scoped to the active tenant."""

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
    """Builder for Cart rows scoped to the active tenant."""

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
def coupon_factory(tenant: Tenant) -> Callable[..., Coupon]:
    """Builder for Coupon rows scoped to the active tenant.

    Defaults to a 10%-off PERCENTAGE coupon with an empty constraints dict
    and no usage cap.  Override via kwargs for per-test customisation.
    """

    def _build(
        code: str | None = None,
        discount_type: str = Coupon.DiscountType.PERCENTAGE,
        value: Decimal | str | int = Decimal("10"),
        currency: str | None = None,
        constraints: dict[str, Any] | None = None,
        usage_limit: int | None = None,
        used_count: int = 0,
        is_active: bool = True,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> Coupon:
        # FIXED coupons must carry a currency (DB CHECK
        # ck_coupon_fixed_requires_currency); PERCENTAGE coupons must not.
        if discount_type == Coupon.DiscountType.FIXED and currency is None:
            currency = "USD"
        if discount_type == Coupon.DiscountType.PERCENTAGE:
            currency = None

        return Coupon.objects.create(
            code=code or f"COUPON-{uuid.uuid4().hex[:8].upper()}",
            discount_type=discount_type,
            value=Decimal(str(value)),
            currency=currency,
            constraints=constraints or {},
            usage_limit=usage_limit,
            used_count=used_count,
            is_active=is_active,
            starts_at=starts_at,
            ends_at=ends_at,
        )

    return _build


@pytest.fixture
def address_factory(tenant: Tenant) -> Callable[..., Address]:
    """Builder for Address rows scoped to the active tenant.

    Defaults to a non-default address — pass ``is_default=True`` to make it
    the customer's default (the partial unique constraint guarantees at most
    one live default per (tenant, user)).
    """

    def _build(
        user_id: uuid.UUID,
        country: str = "US",
        city: str = "Springfield",
        details: str = "1 Main St",
        is_default: bool = False,
        label: str = "",
    ) -> Address:
        return Address.objects.create(
            user_id=user_id,
            country=country,
            city=city,
            details=details,
            is_default=is_default,
            label=label,
        )

    return _build


@pytest.fixture
def utc_now() -> datetime:
    """A stable, timezone-aware ``now`` for deterministic validity-window tests."""
    # Pin to a fixed instant so tests don't drift across DST boundaries.
    return datetime(2026, 5, 6, 12, 0, 0, tzinfo=dt_timezone.utc)


@pytest.fixture
def utc_offset() -> Callable[[int], datetime]:
    """Helper: return a tz-aware datetime ``minutes`` away from a stable now.

    Positive minutes -> future, negative -> past.  Used by the validator
    unit tests to construct ``starts_at`` / ``ends_at`` relative to a fixed
    reference instant without tying to wall-clock time.
    """
    base = datetime(2026, 5, 6, 12, 0, 0, tzinfo=dt_timezone.utc)

    def _at(minutes: int) -> datetime:
        return base + timedelta(minutes=minutes)

    return _at
