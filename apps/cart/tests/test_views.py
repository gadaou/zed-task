"""API-level tests for the cart action endpoints.

Covers ``GET /api/v1/cart`` and all seven ``POST /api/v1/cart/*`` actions.

Every request must include:
- ``X-Tenant-Domain`` header — resolved by ``TenantMiddleware``.
- ``X-User-Id`` header — the interim user-identity contract (UUID).

Test categories per endpoint
-----------------------------
* Happy path — correct status + structured response body.
* Missing ``X-User-Id`` — 400 validation/user-id-required.
* Invalid ``X-User-Id`` — 400 validation/user-id-required.
* Missing ``X-Tenant-Domain`` — 400 tenant/missing-header (middleware).
* Input validation — 400 validation/invalid-input for bad request bodies.
* Domain errors — 404/422 as appropriate.
* Checkout specifics — missing Idempotency-Key (400), missing selections (422),
  idempotent replay (same key + same body → same 202).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.addresses.models import Address
from apps.cart.models import Cart, CartItem
from apps.cart.services import add_product_to_cart
from apps.catalog.models import Product
from apps.coupon.models import Coupon
from apps.payment.models import PaymentMethod
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(fake_redis, monkeypatch):
    """DRF APIClient with fakeredis injected (needed for checkout)."""
    monkeypatch.setattr("apps.core.redis.get_redis_client", lambda: fake_redis)
    return APIClient()


@pytest.fixture
def fake_redis():
    import fakeredis
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def product(tenant) -> Product:
    return Product.objects.create(
        name="Widget",
        price=Decimal("25.00"),
        currency="USD",
        stock=10,
    )


@pytest.fixture
def coupon(tenant) -> Coupon:
    return Coupon.objects.create(
        code="SAVE10",
        discount_type=Coupon.DiscountType.PERCENTAGE,
        value=Decimal("10"),
    )


@pytest.fixture
def payment_method(tenant) -> PaymentMethod:
    return PaymentMethod.objects.create(gateway_slug="dummy_success")


@pytest.fixture
def address(tenant, user_id) -> Address:
    return Address.objects.create(
        user_id=user_id,
        country="US",
        city="Springfield",
        details="742 Evergreen Terrace",
        is_default=True,
    )


@pytest.fixture
def dispatched(monkeypatch):
    """Capture payment IDs dispatched to the Celery queue."""
    calls = []
    monkeypatch.setattr(
        "apps.order.services.enqueue_authorize_payment",
        lambda pid: calls.append(pid),
    )
    return calls


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

BASE = "/api/v1/cart"


def _headers(domain: str, user: uuid.UUID | None = None) -> dict:
    h = {"HTTP_X_TENANT_DOMAIN": domain}
    if user is not None:
        h["HTTP_X_USER_ID"] = str(user)
    return h


# ---------------------------------------------------------------------------
# GET /api/v1/cart
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_cart_creates_active_cart(api_client, tenant, user_id):
    assert Cart.objects.filter(user_id=user_id).count() == 0
    resp = api_client.get(BASE + "/", **_headers(tenant.domain, user_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(user_id)
    assert body["status"] == "ACTIVE"
    assert Cart.objects.filter(user_id=user_id, status="ACTIVE").count() == 1


@pytest.mark.django_db
def test_get_cart_returns_existing_cart(api_client, tenant, user_id):
    cart = Cart.objects.create(user_id=user_id)
    resp = api_client.get(BASE + "/", **_headers(tenant.domain, user_id))
    assert resp.status_code == 200
    assert resp.json()["id"] == str(cart.id)


@pytest.mark.django_db
def test_get_cart_missing_user_id(api_client, tenant):
    resp = api_client.get(BASE + "/", **_headers(tenant.domain))
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


@pytest.mark.django_db
def test_get_cart_invalid_user_id(api_client, tenant):
    resp = api_client.get(
        BASE + "/",
        HTTP_X_TENANT_DOMAIN=tenant.domain,
        HTTP_X_USER_ID="not-a-uuid",
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


@pytest.mark.django_db
def test_get_cart_missing_tenant(api_client, user_id):
    resp = api_client.get(BASE + "/", HTTP_X_USER_ID=str(user_id))
    assert resp.status_code == 400
    assert "missing-header" in resp.json().get("type", "")


# ---------------------------------------------------------------------------
# POST /api/v1/cart/add-product
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_add_product_happy_path(api_client, tenant, user_id, product):
    resp = api_client.post(
        BASE + "/add-product",
        data={"product_id": str(product.id), "quantity": 2},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 2
    assert Decimal(body["total_price"]) == Decimal("50.00")


@pytest.mark.django_db(transaction=True)
def test_add_product_missing_user_id(api_client, tenant, product):
    resp = api_client.post(
        BASE + "/add-product",
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_add_product_missing_fields(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/add-product",
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400
    assert "errors" in resp.json()


@pytest.mark.django_db(transaction=True)
def test_add_product_zero_quantity_rejected(api_client, tenant, user_id, product):
    resp = api_client.post(
        BASE + "/add-product",
        data={"product_id": str(product.id), "quantity": 0},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_add_product_unknown_product_returns_404(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/add-product",
        data={"product_id": str(uuid.uuid4()), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 404
    assert "product/not-found" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_add_product_twice_merges_quantity(api_client, tenant, user_id, product):
    api_client.post(
        BASE + "/add-product",
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    resp = api_client.post(
        BASE + "/add-product",
        data={"product_id": str(product.id), "quantity": 3},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["quantity"] == 4
    assert CartItem.objects.count() == 1


# ---------------------------------------------------------------------------
# POST /api/v1/cart/remove-product
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_remove_product_happy_path(api_client, tenant, user_id, product):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=1)

    resp = api_client.post(
        BASE + "/remove-product",
        data={"product_id": str(product.id)},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert Decimal(resp.json()["total_price"]) == Decimal("0.00")


@pytest.mark.django_db(transaction=True)
def test_remove_product_idempotent(api_client, tenant, user_id):
    """Removing a product not in the cart should not raise — idempotent."""
    resp = api_client.post(
        BASE + "/remove-product",
        data={"product_id": str(uuid.uuid4())},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_remove_product_missing_user_id(api_client, tenant):
    resp = api_client.post(
        BASE + "/remove-product",
        data={"product_id": str(uuid.uuid4())},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/cart/apply-coupon
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_apply_coupon_happy_path(api_client, tenant, user_id, product, coupon):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=2)

    resp = api_client.post(
        BASE + "/apply-coupon",
        data={"code": coupon.code},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["applied_coupons"]) == 1
    assert body["applied_coupons"][0]["code"] == coupon.code
    # 10 % of 50.00 = 5.00
    assert Decimal(body["discount_amount"]) == Decimal("5.00")
    assert Decimal(body["total_after_discount"]) == Decimal("45.00")


@pytest.mark.django_db(transaction=True)
def test_apply_coupon_unknown_code_returns_422(api_client, tenant, user_id, product):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=1)

    resp = api_client.post(
        BASE + "/apply-coupon",
        data={"code": "DOESNOTEXIST"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "coupon/not-found" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_apply_coupon_missing_code(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/apply-coupon",
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/cart/remove-coupon
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_remove_coupon_happy_path(api_client, tenant, user_id, product, coupon):
    from apps.coupon.services import CouponService
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=2)
    CouponService().apply_coupon_to_cart(cart, coupon.code)

    resp = api_client.post(
        BASE + "/remove-coupon",
        data={"coupon_id": str(coupon.id)},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["applied_coupons"] == []
    assert Decimal(resp.json()["discount_amount"]) == Decimal("0.00")


@pytest.mark.django_db(transaction=True)
def test_remove_coupon_idempotent(api_client, tenant, user_id):
    """Removing a coupon not on the cart is a no-op (idempotent)."""
    resp = api_client.post(
        BASE + "/remove-coupon",
        data={"coupon_id": str(uuid.uuid4())},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/cart/add-address
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_add_address_happy_path(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/add-address",
        data={"country": "US", "city": "Springfield", "details": "742 Evergreen Terrace"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected_address"] is not None
    assert body["selected_address"]["country"] == "US"
    # Address should now be saved in the DB
    assert Address.objects.filter(user_id=user_id).count() == 1


@pytest.mark.django_db(transaction=True)
def test_add_address_sets_selected_on_cart(api_client, tenant, user_id):
    api_client.post(
        BASE + "/add-address",
        data={"country": "SA", "city": "Riyadh", "details": "King Fahd Road"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    cart = Cart.objects.get(user_id=user_id, status="ACTIVE")
    assert cart.selected_address_id is not None


@pytest.mark.django_db(transaction=True)
def test_add_address_invalid_country_code(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/add-address",
        data={"country": "USA", "city": "NY", "details": "5th Ave"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_add_address_missing_fields(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/add-address",
        data={"country": "US"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/cart/add-payment-method
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_add_payment_method_happy_path(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/add-payment-method",
        data={"gateway_slug": "dummy_success"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected_payment_method"] is not None
    assert body["selected_payment_method"]["gateway_slug"] == "dummy_success"
    assert PaymentMethod.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_add_payment_method_unknown_gateway(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/add-payment-method",
        data={"gateway_slug": "stripe_not_registered"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "gateway-unavailable" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_add_payment_method_missing_slug(api_client, tenant, user_id):
    resp = api_client.post(
        BASE + "/add-payment-method",
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/cart/checkout
# ---------------------------------------------------------------------------


@pytest.fixture
def ready_cart(tenant, user_id, product, payment_method, address):
    """A cart with one product, a selected address, and a selected payment method."""
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=2)
    cart.refresh_from_db()
    cart.selected_address = address
    cart.selected_payment_method = payment_method
    cart.save(update_fields=["selected_address", "selected_payment_method"])
    return cart


@pytest.mark.django_db(transaction=True)
def test_checkout_returns_202(api_client, tenant, user_id, ready_cart, dispatched):
    resp = api_client.post(
        BASE + "/checkout",
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert "order_id" in body
    assert "payment_id" in body
    assert body["payment_status"] == "pending"


@pytest.mark.django_db(transaction=True)
def test_checkout_missing_idempotency_key(api_client, tenant, user_id, ready_cart):
    resp = api_client.post(
        BASE + "/checkout",
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400
    assert "idempotency-key-required" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_invalid_idempotency_key(api_client, tenant, user_id, ready_cart):
    resp = api_client.post(
        BASE + "/checkout",
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY="not-a-uuid",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400
    assert "idempotency-key-invalid" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_no_selected_address(api_client, tenant, user_id, product, payment_method):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=1)
    cart.selected_payment_method = payment_method
    cart.save(update_fields=["selected_payment_method"])

    resp = api_client.post(
        BASE + "/checkout",
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "checkout-incomplete" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_no_selected_payment_method(api_client, tenant, user_id, product, address):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=1)
    cart.selected_address = address
    cart.save(update_fields=["selected_address"])

    resp = api_client.post(
        BASE + "/checkout",
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "checkout-incomplete" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_missing_user_id(api_client, tenant, ready_cart):
    resp = api_client.post(
        BASE + "/checkout",
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_idempotent_replay(api_client, tenant, user_id, ready_cart, dispatched):
    key = str(uuid.uuid4())

    r1 = api_client.post(
        BASE + "/checkout",
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
        **_headers(tenant.domain, user_id),
    )
    r2 = api_client.post(
        BASE + "/checkout",
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
        **_headers(tenant.domain, user_id),
    )
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["order_id"] == r2.json()["order_id"]
