"""API-level tests for the canonical RESTful cart endpoints.

Covers all new resource-oriented routes under /api/v1/cart/... and also
regression-guards that the legacy action-style routes still work.

Canonical endpoints tested
--------------------------
POST   /api/v1/cart/items/
DELETE /api/v1/cart/items/{product_id}/
POST   /api/v1/cart/coupons/
DELETE /api/v1/cart/coupons/{coupon_id}/
PUT    /api/v1/cart/address/
PUT    /api/v1/cart/payment-method/
PUT    /api/v1/cart/business-details/

Legacy regression
-----------------
POST /api/v1/cart/add-product/          still returns 200
POST /api/v1/cart/remove-product/       still returns 200
POST /api/v1/cart/add-coupon/           still returns 200
POST /api/v1/cart/remove-coupon/        still returns 200
POST /api/v1/cart/add-address/          still returns 200
POST /api/v1/cart/add-payment-method/   still returns 200
POST /api/v1/cart/set-business-details/ still returns 200
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.cart.models import Cart, CartItem
from apps.cart.services import add_product_to_cart
from apps.catalog.models import Product
from apps.coupon.models import Coupon
from apps.payment.models import PaymentMethod


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_views.py; conftest.py provides tenant, user_id,
# product_factory, cart_factory)
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


# ---------------------------------------------------------------------------
# URL constants
# ---------------------------------------------------------------------------

BASE = "/api/v1/cart"

# Canonical RESTful paths
_ITEMS = BASE + "/items/"
_COUPONS = BASE + "/coupons/"
_ADDRESS = BASE + "/address/"
_PAYMENT_METHOD = BASE + "/payment-method/"
_BUSINESS_DETAILS = BASE + "/business-details/"

# Legacy action-style paths (regression guards)
_ADD_PRODUCT = BASE + "/add-product/"
_REMOVE_PRODUCT = BASE + "/remove-product/"
_ADD_COUPON = BASE + "/add-coupon/"
_REMOVE_COUPON = BASE + "/remove-coupon/"
_ADD_ADDRESS = BASE + "/add-address/"
_ADD_PAYMENT_METHOD = BASE + "/add-payment-method/"
_SET_BUSINESS_DETAILS = BASE + "/set-business-details/"


def _headers(domain: str, user: uuid.UUID | None = None) -> dict:
    h = {"HTTP_X_TENANT_DOMAIN": domain}
    if user is not None:
        h["HTTP_X_USER_ID"] = str(user)
    return h


# ---------------------------------------------------------------------------
# POST /api/v1/cart/items/
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_items_post_adds_product(api_client, tenant, user_id, product):
    resp = api_client.post(
        _ITEMS,
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
def test_items_post_missing_user_id(api_client, tenant, product):
    resp = api_client.post(
        _ITEMS,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_items_post_unknown_product_returns_404(api_client, tenant, user_id):
    resp = api_client.post(
        _ITEMS,
        data={"product_id": str(uuid.uuid4()), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 404
    assert "product/not-found" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_items_post_missing_fields_returns_400(api_client, tenant, user_id):
    resp = api_client.post(
        _ITEMS,
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400
    assert "errors" in resp.json()


@pytest.mark.django_db(transaction=True)
def test_items_post_zero_quantity_rejected(api_client, tenant, user_id, product):
    resp = api_client.post(
        _ITEMS,
        data={"product_id": str(product.id), "quantity": 0},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_items_post_merges_quantity_on_duplicate(api_client, tenant, user_id, product):
    api_client.post(
        _ITEMS,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    resp = api_client.post(
        _ITEMS,
        data={"product_id": str(product.id), "quantity": 3},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["quantity"] == 4
    assert CartItem.objects.count() == 1


# ---------------------------------------------------------------------------
# DELETE /api/v1/cart/items/{product_id}/
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_items_delete_removes_product(api_client, tenant, user_id, product):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=1)

    resp = api_client.delete(
        f"{_ITEMS}{product.id}/",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert Decimal(resp.json()["total_price"]) == Decimal("0.00")


@pytest.mark.django_db(transaction=True)
def test_items_delete_idempotent_on_missing_product(api_client, tenant, user_id):
    resp = api_client.delete(
        f"{_ITEMS}{uuid.uuid4()}/",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_items_delete_missing_user_id(api_client, tenant):
    resp = api_client.delete(
        f"{_ITEMS}{uuid.uuid4()}/",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


# ---------------------------------------------------------------------------
# POST /api/v1/cart/coupons/
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_coupons_post_applies_coupon(api_client, tenant, user_id, product, coupon):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=2)

    resp = api_client.post(
        _COUPONS,
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
def test_coupons_post_unknown_code_returns_422(api_client, tenant, user_id, product):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=1)

    resp = api_client.post(
        _COUPONS,
        data={"code": "DOESNOTEXIST"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422
    assert "coupon/not-found" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_coupons_post_missing_code_returns_400(api_client, tenant, user_id):
    resp = api_client.post(
        _COUPONS,
        data={},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_coupons_post_missing_user_id(api_client, tenant, coupon):
    resp = api_client.post(
        _COUPONS,
        data={"code": coupon.code},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


# ---------------------------------------------------------------------------
# DELETE /api/v1/cart/coupons/{coupon_id}/
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_coupons_delete_removes_coupon(api_client, tenant, user_id, product, coupon):
    from apps.coupon.services import CouponService

    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=2)
    CouponService().apply_coupon_to_cart(cart, coupon.code)

    resp = api_client.delete(
        f"{_COUPONS}{coupon.id}/",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied_coupons"] == []
    assert Decimal(body["discount_amount"]) == Decimal("0.00")


@pytest.mark.django_db(transaction=True)
def test_coupons_delete_idempotent_on_unapplied_coupon(api_client, tenant, user_id):
    resp = api_client.delete(
        f"{_COUPONS}{uuid.uuid4()}/",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_coupons_delete_missing_user_id(api_client, tenant):
    resp = api_client.delete(
        f"{_COUPONS}{uuid.uuid4()}/",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


# ---------------------------------------------------------------------------
# PUT /api/v1/cart/address/
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_address_put_sets_address(api_client, tenant, user_id):
    resp = api_client.put(
        _ADDRESS,
        data={
            "country": "US",
            "city": "Springfield",
            "details": "742 Evergreen Terrace",
        },
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected_address"] is not None
    assert body["selected_address"]["country"] == "US"
    assert body["selected_address"]["city"] == "Springfield"


@pytest.mark.django_db(transaction=True)
def test_address_put_persists_to_db(api_client, tenant, user_id):
    from apps.addresses.models import Address

    api_client.put(
        _ADDRESS,
        data={"country": "SA", "city": "Riyadh", "details": "King Fahd Road"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    cart = Cart.objects.get(user_id=user_id, status="ACTIVE")
    assert cart.selected_address_id is not None
    assert Address.objects.filter(user_id=user_id).count() == 1


@pytest.mark.django_db(transaction=True)
def test_address_put_invalid_country_code(api_client, tenant, user_id):
    resp = api_client.put(
        _ADDRESS,
        data={"country": "USA", "city": "NY", "details": "5th Ave"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_address_put_missing_user_id(api_client, tenant):
    resp = api_client.put(
        _ADDRESS,
        data={"country": "US", "city": "NY", "details": "5th Ave"},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


# ---------------------------------------------------------------------------
# PUT /api/v1/cart/payment-method/
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_payment_method_put_sets_method(api_client, tenant, user_id):
    resp = api_client.put(
        _PAYMENT_METHOD,
        data={"gateway_slug": "dummy_success"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected_payment_method"] is not None
    assert body["selected_payment_method"]["gateway_slug"] == "dummy_success"


@pytest.mark.django_db(transaction=True)
def test_payment_method_put_unknown_gateway_returns_422(api_client, tenant, user_id):
    resp = api_client.put(
        _PAYMENT_METHOD,
        data={"gateway_slug": "nonexistent_gateway"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 422


@pytest.mark.django_db(transaction=True)
def test_payment_method_put_missing_user_id(api_client, tenant):
    resp = api_client.put(
        _PAYMENT_METHOD,
        data={"gateway_slug": "dummy_success"},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


# ---------------------------------------------------------------------------
# PUT /api/v1/cart/business-details/
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_business_details_put_sets_fields(api_client, tenant, user_id):
    resp = api_client.put(
        _BUSINESS_DETAILS,
        data={
            "company_name": "Acme Corp Ltd",
            "tax_number": "GB123456789",
            "purchase_order_reference": "PO-2026-00042",
        },
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["company_name"] == "Acme Corp Ltd"
    assert body["tax_number"] == "GB123456789"
    assert body["purchase_order_reference"] == "PO-2026-00042"


@pytest.mark.django_db(transaction=True)
def test_business_details_put_partial_fields(api_client, tenant, user_id):
    resp = api_client.put(
        _BUSINESS_DETAILS,
        data={"purchase_order_reference": "PO-2026-00099"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["purchase_order_reference"] == "PO-2026-00099"


@pytest.mark.django_db(transaction=True)
def test_business_details_put_missing_user_id(api_client, tenant):
    resp = api_client.put(
        _BUSINESS_DETAILS,
        data={"company_name": "Acme"},
        format="json",
        **_headers(tenant.domain),
    )
    assert resp.status_code == 400
    assert "user-id-required" in resp.json().get("type", "")


# ---------------------------------------------------------------------------
# Legacy route regression guards — every action-style endpoint still works
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_legacy_add_product_still_works(api_client, tenant, user_id, product):
    resp = api_client.post(
        _ADD_PRODUCT,
        data={"product_id": str(product.id), "quantity": 1},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


@pytest.mark.django_db(transaction=True)
def test_legacy_remove_product_still_works(api_client, tenant, user_id, product):
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


@pytest.mark.django_db(transaction=True)
def test_legacy_add_coupon_still_works(api_client, tenant, user_id, product, coupon):
    cart = Cart.objects.create(user_id=user_id)
    add_product_to_cart(cart, product, quantity=1)

    resp = api_client.post(
        _ADD_COUPON,
        data={"code": coupon.code},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert len(resp.json()["applied_coupons"]) == 1


@pytest.mark.django_db(transaction=True)
def test_legacy_remove_coupon_still_works(api_client, tenant, user_id, product, coupon):
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


@pytest.mark.django_db(transaction=True)
def test_legacy_add_address_still_works(api_client, tenant, user_id):
    resp = api_client.post(
        _ADD_ADDRESS,
        data={"country": "US", "city": "Portland", "details": "1 Oak St"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["selected_address"] is not None


@pytest.mark.django_db(transaction=True)
def test_legacy_add_payment_method_still_works(api_client, tenant, user_id):
    resp = api_client.post(
        _ADD_PAYMENT_METHOD,
        data={"gateway_slug": "dummy_success"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["selected_payment_method"] is not None


@pytest.mark.django_db(transaction=True)
def test_legacy_set_business_details_still_works(api_client, tenant, user_id):
    resp = api_client.post(
        _SET_BUSINESS_DETAILS,
        data={"company_name": "Legacy Corp"},
        format="json",
        **_headers(tenant.domain, user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["company_name"] == "Legacy Corp"
