"""Tests for lightweight B2B support on the cart and checkout flow.

Covers:
- POST /api/v1/cart/set-business-details/ — happy path (all fields, partial)
- Validation — at least one field required
- GET /api/v1/cart/ — B2B fields appear immediately after set-business-details
- B2B fields are optional — existing cart tests not broken
- Checkout copies B2B fields to Order snapshot
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.addresses.models import Address
from apps.cart.models import Cart
from apps.catalog.models import Product
from apps.order.models import Order
from apps.payment.models import PaymentMethod

BASE = "/api/v1/cart"
_SET_BUSINESS = BASE + "/set-business-details/"
_CHECKOUT = BASE + "/checkout/"
_ADD_PRODUCT = BASE + "/add-product/"
_ADD_ADDRESS = BASE + "/add-address/"
_ADD_PAYMENT_METHOD = BASE + "/add-payment-method/"


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
        price=Decimal("50.00"),
        currency="USD",
        stock=10,
    )


@pytest.fixture
def address(tenant, user_id) -> Address:
    return Address.objects.create(
        user_id=user_id,
        country="US",
        city="Chicago",
        details="123 Main St",
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
# POST /api/v1/cart/set-business-details/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_set_business_details_all_fields(api_client, tenant, user_id):
    """All three B2B fields are persisted and returned in the cart response."""
    resp = api_client.post(
        _SET_BUSINESS,
        data={
            "company_name": "Acme Corp Ltd",
            "tax_number": "GB123456789",
            "purchase_order_reference": "PO-2026-00042",
        },
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200, resp.data
    data = resp.data
    assert data["company_name"] == "Acme Corp Ltd"
    assert data["tax_number"] == "GB123456789"
    assert data["purchase_order_reference"] == "PO-2026-00042"

    # Persisted in the database
    cart = Cart.objects.get(user_id=user_id)
    assert cart.company_name == "Acme Corp Ltd"
    assert cart.tax_number == "GB123456789"
    assert cart.purchase_order_reference == "PO-2026-00042"


@pytest.mark.django_db
def test_set_business_details_partial_fields(api_client, tenant, user_id):
    """A single field is sufficient — the others default to empty string."""
    resp = api_client.post(
        _SET_BUSINESS,
        data={"purchase_order_reference": "PO-2026-00099"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200, resp.data
    assert resp.data["purchase_order_reference"] == "PO-2026-00099"
    assert resp.data["company_name"] == ""
    assert resp.data["tax_number"] == ""


@pytest.mark.django_db
def test_set_business_details_overwrite(api_client, tenant, user_id):
    """Calling set-business-details twice overwrites the previous values."""
    api_client.post(
        _SET_BUSINESS,
        data={"company_name": "Old Corp"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    resp = api_client.post(
        _SET_BUSINESS,
        data={"company_name": "New Corp", "tax_number": "DE987654321"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.data["company_name"] == "New Corp"
    assert resp.data["tax_number"] == "DE987654321"
    assert resp.data["purchase_order_reference"] == ""


@pytest.mark.django_db
def test_set_business_details_all_empty_returns_400(api_client, tenant, user_id):
    """All fields empty/absent must return 400 (no-op guard)."""
    resp = api_client.post(
        _SET_BUSINESS,
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_set_business_details_missing_user_id(api_client, tenant):
    """Missing X-User-Id header returns 400."""
    resp = api_client.post(
        _SET_BUSINESS,
        data={"company_name": "Corp"},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert resp.data["type"].endswith("user-id-required")


# ---------------------------------------------------------------------------
# GET /api/v1/cart/ — B2B fields visible after set-business-details
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_cart_returns_b2b_fields_after_set(api_client, tenant, user_id):
    """GET /cart/ returns B2B fields immediately after set-business-details."""
    api_client.post(
        _SET_BUSINESS,
        data={"company_name": "My Company", "tax_number": "AU12345678"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    resp = api_client.get(BASE + "/", **_headers(tenant.domain, user_id))
    assert resp.status_code == 200
    assert resp.data["company_name"] == "My Company"
    assert resp.data["tax_number"] == "AU12345678"
    assert resp.data["purchase_order_reference"] == ""


@pytest.mark.django_db
def test_get_cart_b2b_fields_default_empty(api_client, tenant, user_id):
    """GET /cart/ returns empty strings for B2B fields when never set (B2C flow)."""
    resp = api_client.get(BASE + "/", **_headers(tenant.domain, user_id))
    assert resp.status_code == 200
    assert resp.data["company_name"] == ""
    assert resp.data["tax_number"] == ""
    assert resp.data["purchase_order_reference"] == ""


# ---------------------------------------------------------------------------
# Checkout snapshots B2B fields onto Order
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_checkout_snapshots_b2b_fields_to_order(
    api_client, tenant, user_id, product, address, payment_method, dispatched
):
    """Order created at checkout carries the cart's B2B fields."""
    # Set B2B details
    api_client.post(
        _SET_BUSINESS,
        data={
            "company_name": "Acme Corp",
            "tax_number": "GB000000000",
            "purchase_order_reference": "PO-TEST-001",
        },
        format="json",
        **_headers(tenant.domain, user_id),
    )
    # Add product
    api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    # Add address and payment method via cart
    api_client.post(
        _ADD_ADDRESS,
        data={"country": "US", "city": "NYC", "details": "1 Main St"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    api_client.post(
        _ADD_PAYMENT_METHOD,
        data={"gateway_slug": "dummy_success"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    # Checkout
    idem_key = str(uuid.uuid4())
    resp = api_client.post(
        _CHECKOUT,
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
        HTTP_IDEMPOTENCY_KEY=idem_key,
    )
    assert resp.status_code == 202, resp.data
    order = Order.objects.get(id=resp.data["order_id"])
    assert order.company_name == "Acme Corp"
    assert order.tax_number == "GB000000000"
    assert order.purchase_order_reference == "PO-TEST-001"


@pytest.mark.django_db
def test_checkout_b2b_fields_empty_for_b2c(
    api_client, tenant, user_id, product, address, payment_method, dispatched
):
    """Order created for B2C checkout has empty B2B fields — not null, not error."""
    api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    api_client.post(
        _ADD_ADDRESS,
        data={"country": "US", "city": "LA", "details": "2 Oak Ave"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    api_client.post(
        _ADD_PAYMENT_METHOD,
        data={"gateway_slug": "dummy_success"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    idem_key = str(uuid.uuid4())
    resp = api_client.post(
        _CHECKOUT,
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
        HTTP_IDEMPOTENCY_KEY=idem_key,
    )
    assert resp.status_code == 202, resp.data
    order = Order.objects.get(id=resp.data["order_id"])
    assert order.company_name == ""
    assert order.tax_number == ""
    assert order.purchase_order_reference == ""
