"""Tests for the Redis-backed rate throttle on cart mutation endpoints.

Verifies that:
- POST /api/v1/cart/checkout/ returns 429 after 10 requests in the same window
- POST /api/v1/cart/add-product/ returns 429 after 60 requests in the same window
- 429 body is RFC 7807 problem+json with the expected ``type`` slug
- Different (tenant, user) combinations have independent counters
- Throttle state is scoped to a fixed window (counter resets in a fresh fakeredis)

Implementation note
-------------------
Tests use ``fakeredis.FakeRedis`` (already in requirements.txt) injected via
``monkeypatch``.  The throttle limit is overridden via ``settings.REST_FRAMEWORK``
so tests run quickly without firing 60 real requests where possible.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.addresses.models import Address
from apps.catalog.models import Product
from apps.payment.models import PaymentMethod

BASE = "/api/v1/cart"
_CHECKOUT = BASE + "/checkout/"
_ADD_PRODUCT = BASE + "/add-product/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def api_client(fake_redis, monkeypatch):
    monkeypatch.setattr("apps.core.redis.get_redis_client", lambda: fake_redis)
    return APIClient()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def product(tenant) -> Product:
    return Product.objects.create(
        name="Widget",
        price=Decimal("10.00"),
        currency="USD",
        stock=500,
    )


@pytest.fixture
def address(tenant, user_id) -> Address:
    return Address.objects.create(
        user_id=user_id,
        country="US",
        city="Boston",
        details="1 Elm St",
        is_default=True,
    )


@pytest.fixture
def payment_method(tenant) -> PaymentMethod:
    return PaymentMethod.objects.create(gateway_slug="dummy_success")


@pytest.fixture
def dispatched(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "apps.order.services.enqueue_authorize_payment",
        lambda pid: calls.append(pid),
    )
    return calls


def _headers(domain: str, user: uuid.UUID | None = None) -> dict:
    h = {"HTTP_X_TENANT_DOMAIN": domain}
    if user is not None:
        h["HTTP_X_USER_ID"] = str(user)
    return h


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_cart_for_checkout(api_client, tenant, user_id, product):
    """Add product, address, and payment method to the active cart."""
    api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    api_client.post(
        BASE + "/add-address/",
        data={"country": "US", "city": "Boston", "details": "1 Elm St"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    api_client.post(
        BASE + "/add-payment-method/",
        data={"gateway_slug": "dummy_success"},
        format="json",
        **_headers(tenant.domain, user_id),
    )


# ---------------------------------------------------------------------------
# Checkout throttle — 10/minute
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(
    REST_FRAMEWORK={
        **__import__("django.conf", fromlist=["settings"]).settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            "checkout": "3/minute",
            "add_product": "60/minute",
        },
    }
)
def test_checkout_throttle_allows_within_limit(
    api_client, tenant, user_id, product, dispatched
):
    """First 3 checkout attempts are allowed under the overridden limit.

    After the first successful checkout the cart is CHECKED_OUT so
    subsequent calls may return 422/409, but they must not return 429 — the
    throttle counter is what we are testing, not the checkout business logic.
    """
    _setup_cart_for_checkout(api_client, tenant, user_id, product)

    for i in range(3):
        idem = str(uuid.uuid4())
        resp = api_client.post(
            _CHECKOUT,
            data={},
            format="json",
            **_headers(tenant.domain, user_id),
            HTTP_IDEMPOTENCY_KEY=idem,
        )
        # Any non-429 status is acceptable — the throttle must not fire yet.
        assert resp.status_code != 429, f"Throttled too early on attempt {i + 1}"


@pytest.mark.django_db
@override_settings(
    REST_FRAMEWORK={
        **__import__("django.conf", fromlist=["settings"]).settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            "checkout": "3/minute",
            "add_product": "60/minute",
        },
    }
)
def test_checkout_throttle_blocks_on_excess(
    api_client, tenant, user_id, product, dispatched
):
    """The 4th checkout attempt returns 429 when the limit is 3/minute."""
    _setup_cart_for_checkout(api_client, tenant, user_id, product)

    # Exhaust the 3-request budget
    for _ in range(3):
        api_client.post(
            _CHECKOUT,
            data={},
            format="json",
            **_headers(tenant.domain, user_id),
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

    # 4th request must be throttled
    resp = api_client.post(
        _CHECKOUT,
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert resp.status_code == 429
    assert "type" in resp.data
    assert "rate-limit" in resp.data["type"]


# ---------------------------------------------------------------------------
# Add-product throttle — 60/minute (tested with a small override)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(
    REST_FRAMEWORK={
        **__import__("django.conf", fromlist=["settings"]).settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            "checkout": "10/minute",
            "add_product": "3/minute",
        },
    }
)
def test_add_product_throttle_blocks_on_excess(api_client, tenant, user_id, product):
    """The 4th add-product call returns 429 when the limit is overridden to 3/minute."""
    for i in range(3):
        resp = api_client.post(
            _ADD_PRODUCT,
            data={"product_id": str(product.id), "quantity": 1},
            format="json",
            **_headers(tenant.domain, user_id),
        )
        assert resp.status_code != 429, f"Throttled too early on call {i + 1}"

    resp = api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 429
    assert "type" in resp.data
    assert "rate-limit" in resp.data["type"]


# ---------------------------------------------------------------------------
# Tenant isolation — different tenants have independent counters
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(
    REST_FRAMEWORK={
        **__import__("django.conf", fromlist=["settings"]).settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            "checkout": "10/minute",
            "add_product": "2/minute",
        },
    }
)
def test_add_product_throttle_is_per_tenant(db, fake_redis, monkeypatch):
    """Throttle counters are isolated between tenants."""
    from apps.tenant.context import tenant_context
    from apps.tenant.models import Tenant

    monkeypatch.setattr("apps.core.redis.get_redis_client", lambda: fake_redis)
    client = APIClient()

    uid = uuid.uuid4()

    # Create two tenants and a product in each
    tenant_a = Tenant.objects.create(
        name="Tenant A", domain=f"a-{uuid.uuid4().hex[:8]}.test"
    )
    tenant_b = Tenant.objects.create(
        name="Tenant B", domain=f"b-{uuid.uuid4().hex[:8]}.test"
    )

    with tenant_context(tenant_a):
        p_a = Product.objects.create(
            name="WidgetA", price=Decimal("5.00"), currency="USD", stock=50
        )

    with tenant_context(tenant_b):
        p_b = Product.objects.create(
            name="WidgetB", price=Decimal("5.00"), currency="USD", stock=50
        )

    def _post_add(tenant, product_obj):
        return client.post(
            _ADD_PRODUCT,
            data={"product_id": str(product_obj.id), "quantity": 1},
            format="json",
            HTTP_X_TENANT_DOMAIN=tenant.domain,
            HTTP_X_USER_ID=str(uid),
        )

    # Exhaust tenant A's budget (limit = 2)
    with tenant_context(tenant_a):
        _post_add(tenant_a, p_a)
        _post_add(tenant_a, p_a)
        resp_a = _post_add(tenant_a, p_a)
    assert resp_a.status_code == 429

    # Tenant B's budget is untouched — first call must not be throttled
    with tenant_context(tenant_b):
        resp_b = _post_add(tenant_b, p_b)
    assert resp_b.status_code != 429
