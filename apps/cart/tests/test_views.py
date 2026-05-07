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

# URL path constants — all action paths use trailing slashes per Django convention.
_ADD_PRODUCT = BASE + "/add-product/"
_REMOVE_PRODUCT = BASE + "/remove-product/"
_ADD_COUPON = BASE + "/add-coupon/"
_REMOVE_COUPON = BASE + "/remove-coupon/"
_ADD_ADDRESS = BASE + "/add-address/"
_ADD_PAYMENT_METHOD = BASE + "/add-payment-method/"
_CHECKOUT = BASE + "/checkout/"


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
        _ADD_PRODUCT,
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
        _ADD_PRODUCT,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_add_product_missing_fields(api_client, tenant, user_id):
    resp = api_client.post(
        _ADD_PRODUCT,
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400
    assert "errors" in resp.json()


@pytest.mark.django_db(transaction=True)
def test_add_product_zero_quantity_rejected(api_client, tenant, user_id, product):
    resp = api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(product.id), "quantity": 0},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_add_product_unknown_product_returns_404(api_client, tenant, user_id):
    resp = api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(uuid.uuid4()), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 404
    assert "product/not-found" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_add_product_twice_merges_quantity(api_client, tenant, user_id, product):
    api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    resp = api_client.post(
        _ADD_PRODUCT,
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
        _REMOVE_PRODUCT,
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
        _REMOVE_PRODUCT,
        data={"product_id": str(uuid.uuid4())},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_remove_product_missing_user_id(api_client, tenant):
    resp = api_client.post(
        _REMOVE_PRODUCT,
        data={"product_id": str(uuid.uuid4())},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v1/cart/add-coupon
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_apply_coupon_happy_path(api_client, tenant, user_id, product, coupon):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=2)

    resp = api_client.post(
        _ADD_COUPON,
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
        _ADD_COUPON,
        data={"code": "DOESNOTEXIST"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "coupon/not-found" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_apply_coupon_missing_code(api_client, tenant, user_id):
    resp = api_client.post(
        _ADD_COUPON,
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
        _REMOVE_COUPON,
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
        _REMOVE_COUPON,
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
        _ADD_ADDRESS,
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
        _ADD_ADDRESS,
        data={"country": "SA", "city": "Riyadh", "details": "King Fahd Road"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    cart = Cart.objects.get(user_id=user_id, status="ACTIVE")
    assert cart.selected_address_id is not None


@pytest.mark.django_db(transaction=True)
def test_add_address_invalid_country_code(api_client, tenant, user_id):
    resp = api_client.post(
        _ADD_ADDRESS,
        data={"country": "USA", "city": "NY", "details": "5th Ave"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_add_address_missing_fields(api_client, tenant, user_id):
    resp = api_client.post(
        _ADD_ADDRESS,
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
        _ADD_PAYMENT_METHOD,
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
        _ADD_PAYMENT_METHOD,
        data={"gateway_slug": "stripe_not_registered"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "gateway-unavailable" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_add_payment_method_missing_slug(api_client, tenant, user_id):
    resp = api_client.post(
        _ADD_PAYMENT_METHOD,
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
        _CHECKOUT,
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
        _CHECKOUT,
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400
    assert "idempotency-key-required" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_invalid_idempotency_key(api_client, tenant, user_id, ready_cart):
    resp = api_client.post(
        _CHECKOUT,
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
        _CHECKOUT,
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
        _CHECKOUT,
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
        _CHECKOUT,
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
        _CHECKOUT,
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
        **_headers(tenant.domain, user_id),
    )
    r2 = api_client.post(
        _CHECKOUT,
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
        **_headers(tenant.domain, user_id),
    )
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["order_id"] == r2.json()["order_id"]


# ---------------------------------------------------------------------------
# POST /api/v1/carts/{cart_id}/checkout/  — legacy resource-oriented endpoint
# Ownership and cross-tenant enforcement
# ---------------------------------------------------------------------------

CARTS_BASE = "/api/v1/carts"


@pytest.mark.django_db(transaction=True)
def test_cart_id_checkout_rejects_wrong_user_id(
    api_client, tenant, user_id, ready_cart, dispatched
):
    """When X-User-Id does not match cart.user_id the response is 403."""
    other_user = uuid.uuid4()
    resp = api_client.post(
        f"{CARTS_BASE}/{ready_cart.id}/checkout/",
        data={
            "payment_method_id": str(ready_cart.selected_payment_method_id),
            "address_id": str(ready_cart.selected_address_id),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain, other_user),
    )
    assert resp.status_code == 403
    assert "cart/forbidden" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_cart_id_checkout_succeeds_for_correct_user_id(
    api_client, tenant, user_id, ready_cart, dispatched
):
    """CheckoutView accepts the request when X-User-Id matches cart.user_id."""
    resp = api_client.post(
        f"{CARTS_BASE}/{ready_cart.id}/checkout/",
        data={
            "payment_method_id": str(ready_cart.selected_payment_method_id),
            "address_id": str(ready_cart.selected_address_id),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 202


@pytest.mark.django_db(transaction=True)
def test_checkout_empty_cart_returns_422(api_client, tenant, user_id, address, payment_method):
    """CartCheckoutView maps CartEmpty → 422 cart/empty for the action endpoint."""
    cart = Cart.objects.create(user_id=user_id)
    cart.selected_address = address
    cart.selected_payment_method = payment_method
    cart.save(update_fields=["selected_address", "selected_payment_method"])

    resp = api_client.post(
        _CHECKOUT,
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "cart/empty" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_oos_returns_422(api_client, tenant, user_id, address, payment_method):
    """CartCheckoutView maps ProductOutOfStock → 422 product/out-of-stock for the action endpoint."""
    from apps.catalog.models import Product as _Product

    low_stock = _Product.objects.create(
        name="Low-stock Widget",
        price=Decimal("10.00"),
        currency="USD",
        stock=1,
    )
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, low_stock, quantity=2)  # exceeds available stock
    cart.refresh_from_db()
    cart.selected_address = address
    cart.selected_payment_method = payment_method
    cart.save(update_fields=["selected_address", "selected_payment_method"])

    resp = api_client.post(
        _CHECKOUT,
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "out-of-stock" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_tenant_a_cannot_checkout_tenant_b_cart(api_client, monkeypatch):
    """A cart_id that belongs to tenant B is not found when queried under tenant A."""
    import fakeredis
    monkeypatch.setattr("apps.core.redis.get_redis_client", lambda: fakeredis.FakeRedis(decode_responses=True))

    from apps.tenant.models import Tenant
    from apps.tenant.context import tenant_context

    tenant_a = Tenant.objects.create(name="A", domain=f"a-{uuid.uuid4().hex[:8]}.test")
    tenant_b = Tenant.objects.create(name="B", domain=f"b-{uuid.uuid4().hex[:8]}.test")

    user_b = uuid.uuid4()
    with tenant_context(tenant_b):
        cart_b = Cart.objects.create(user_id=user_b)

    user_a = uuid.uuid4()
    resp = api_client.post(
        f"{CARTS_BASE}/{cart_b.id}/checkout/",
        data={
            "payment_method_id": str(uuid.uuid4()),
            "address_id": str(uuid.uuid4()),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        HTTP_X_TENANT_DOMAIN=tenant_a.domain,
        HTTP_X_USER_ID=str(user_a),
    )
    assert resp.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_tenant_a_cannot_add_tenant_b_product(api_client):
    """POST /cart/add-product/ with a product_id from another tenant returns 404.

    Verifies the full stack: TenantMiddleware resolves tenant_a from the
    X-Tenant-Domain header → TenantAwareManager injects WHERE tenant_id=tenant_a
    → Product.objects.get(pk=product_b.id) raises DoesNotExist → view returns
    404 product/not-found.  No cross-tenant data leaks.
    """
    from apps.tenant.models import Tenant
    from apps.tenant.context import tenant_context

    tenant_a = Tenant.objects.create(name="A", domain=f"a-{uuid.uuid4().hex[:8]}.test")
    tenant_b = Tenant.objects.create(name="B", domain=f"b-{uuid.uuid4().hex[:8]}.test")

    with tenant_context(tenant_b):
        product_b = Product.objects.create(
            name="B-only widget",
            price=Decimal("10.00"),
            currency="USD",
            stock=5,
        )

    user_a = uuid.uuid4()
    resp = api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(product_b.id), "quantity": 1},
        format="json",
        HTTP_X_TENANT_DOMAIN=tenant_a.domain,
        HTTP_X_USER_ID=str(user_a),
    )
    assert resp.status_code == 404
    assert "product/not-found" in resp.json().get("type", "")


# ---------------------------------------------------------------------------
# Error shape consistency
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_validation_error_is_rfc7807(api_client, tenant, user_id):
    """Serializer validation failures return a full RFC 7807 problem+json body.

    Ensures ``type`` is a URI, ``title`` and ``status`` are present, and
    the DRF field errors are embedded under the ``errors`` key.
    """
    resp = api_client.post(
        _ADD_PRODUCT,
        data={},  # missing product_id and quantity
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["type"].startswith("https://"), (
        f"type must be a URI, got {body['type']!r}"
    )
    assert "validation/invalid-input" in body["type"]
    assert "title" in body, "RFC 7807 body must include 'title'"
    assert body["status"] == 400, "RFC 7807 body must mirror the HTTP status code"
    assert "errors" in body, "Validation response must embed DRF field errors"


# ---------------------------------------------------------------------------
# CartCheckoutView — 409 conflict scenarios
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_checkout_action_409_on_lock_conflict(
    api_client, tenant, user_id, ready_cart, monkeypatch
):
    """CartCheckoutView returns 409 cart/locked when the Redis lock is unavailable."""
    from contextlib import contextmanager
    from apps.core.exceptions import LockNotAcquired

    @contextmanager
    def _lock_always_fails(_key, _ttl, *, client=None):
        raise LockNotAcquired("lock already held by concurrent request")
        yield  # noqa: unreachable

    monkeypatch.setattr("apps.order.services.redis_lock", _lock_always_fails)

    resp = api_client.post(
        _CHECKOUT,
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert "cart/locked" in body.get("type", ""), (
        f"Expected 'cart/locked' in type, got {body.get('type')!r}"
    )
