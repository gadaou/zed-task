"""API-level tests for ``POST /api/v1/carts/{cart_id}/checkout/``.

Uses DRF's ``APIClient`` for a realistic HTTP-in-process test that exercises
the view, serializer, and exception-to-HTTP mapping in one shot.

Test coverage:
    Happy path          — 202 Accepted with correct body fields.
    Missing header      — 400 when Idempotency-Key is absent.
    Invalid header      — 400 when Idempotency-Key is not a valid UUID.
    Invalid body        — 400 on missing required fields.
    Idempotent replay   — second identical call returns same 202 body.
    Conflict            — same key + different body → 409.
    Cart not found      — 404.
    Cart locked (OOS)   — 422 with product/out-of-stock.
    Empty cart          — 422 with cart/empty.

The ``X-Tenant-Domain`` header must be sent on every request because
``TenantMiddleware`` resolves the tenant from it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.cart.services import add_product_to_cart
from apps.catalog.models import Product
from apps.order.models import Order


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def checkout_url(cart_id: uuid.UUID) -> str:
    return f"/api/v1/carts/{cart_id}/checkout/"


def _post(client: APIClient, cart_id, pm_id, addr_id, idem_key, domain) -> object:
    return client.post(
        checkout_url(cart_id),
        data={"payment_method_id": str(pm_id), "address_id": str(addr_id)},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(idem_key),
        HTTP_X_TENANT_DOMAIN=domain,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(fake_redis, monkeypatch):
    """API client with fakeredis injected."""
    monkeypatch.setattr("apps.core.redis.get_redis_client", lambda: fake_redis)
    return APIClient()


@pytest.fixture
def dispatched(monkeypatch):
    """Record payment_ids dispatched via enqueue_authorize_payment."""
    calls = []
    monkeypatch.setattr(
        "apps.order.services.enqueue_authorize_payment",
        lambda pid: calls.append(pid),
    )
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_checkout_returns_202(api_client, loaded_cart, tenant, dispatched):
    cart, address, pm = loaded_cart
    resp = _post(api_client, cart.id, pm.id, address.id, uuid.uuid4(), tenant.domain)

    assert resp.status_code == 202
    body = resp.json()
    assert "order_id" in body
    assert "payment_id" in body
    assert body["payment_status"] == "pending"
    assert body["currency"] == cart.currency
    assert Order.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_checkout_missing_idempotency_key(api_client, loaded_cart, tenant):
    cart, address, pm = loaded_cart
    resp = api_client.post(
        checkout_url(cart.id),
        data={"payment_method_id": str(pm.id), "address_id": str(address.id)},
        format="json",
        HTTP_X_TENANT_DOMAIN=tenant.domain,
    )
    assert resp.status_code == 400
    assert "idempotency-key-required" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_invalid_idempotency_key(api_client, loaded_cart, tenant):
    cart, address, pm = loaded_cart
    resp = api_client.post(
        checkout_url(cart.id),
        data={"payment_method_id": str(pm.id), "address_id": str(address.id)},
        format="json",
        HTTP_IDEMPOTENCY_KEY="not-a-uuid",
        HTTP_X_TENANT_DOMAIN=tenant.domain,
    )
    assert resp.status_code == 400
    assert "idempotency-key-invalid" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_missing_body_fields(api_client, loaded_cart, tenant):
    cart, _, _ = loaded_cart
    resp = api_client.post(
        checkout_url(cart.id),
        data={},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        HTTP_X_TENANT_DOMAIN=tenant.domain,
    )
    assert resp.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_checkout_idempotent_replay(api_client, loaded_cart, tenant, dispatched):
    cart, address, pm = loaded_cart
    key = str(uuid.uuid4())

    r1 = _post(api_client, cart.id, pm.id, address.id, key, tenant.domain)
    r2 = _post(api_client, cart.id, pm.id, address.id, key, tenant.domain)

    assert r1.status_code == r2.status_code == 202
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert Order.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_checkout_idempotency_conflict(
    api_client, loaded_cart, tenant, address_factory, dispatched
):
    """Same key + different address_id → 409 idempotency/conflict."""
    from apps.addresses.models import Address

    cart, address, pm = loaded_cart
    key = str(uuid.uuid4())

    r1 = _post(api_client, cart.id, pm.id, address.id, key, tenant.domain)
    assert r1.status_code == 202

    # Different address — triggers conflict.
    other_address = Address.objects.create(
        user_id=cart.user_id, country="US", city="NY", details="2 Ave",
    )
    r2 = _post(api_client, cart.id, pm.id, other_address.id, key, tenant.domain)
    assert r2.status_code == 409
    assert "idempotency/conflict" in r2.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_cart_not_found(api_client, tenant, address_factory, payment_method_factory):
    from apps.addresses.models import Address
    user_id = uuid.uuid4()
    address = Address.objects.create(
        user_id=user_id, country="US", city="City", details="St",
    )
    pm = payment_method_factory()
    resp = _post(api_client, uuid.uuid4(), pm.id, address.id, uuid.uuid4(), tenant.domain)
    assert resp.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_checkout_empty_cart_returns_422(api_client, cart_factory, address_factory, payment_method_factory, tenant):
    cart = cart_factory()
    address = address_factory(user_id=cart.user_id)
    pm = payment_method_factory()

    resp = _post(api_client, cart.id, pm.id, address.id, uuid.uuid4(), tenant.domain)
    assert resp.status_code == 422
    assert "cart/empty" in resp.json().get("type", "")


@pytest.mark.django_db(transaction=True)
def test_checkout_out_of_stock_returns_422(
    api_client, cart_factory, product_factory, address_factory, payment_method_factory, tenant
):
    cart = cart_factory()
    product = product_factory(stock=1)
    add_product_to_cart(cart, product, quantity=2)
    cart.refresh_from_db()
    address = address_factory(user_id=cart.user_id)
    pm = payment_method_factory()

    resp = _post(api_client, cart.id, pm.id, address.id, uuid.uuid4(), tenant.domain)
    assert resp.status_code == 422
    assert "product/out-of-stock" in resp.json().get("type", "")
