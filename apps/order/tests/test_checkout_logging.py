"""Tests that checkout lifecycle events produce structured log records.

These tests assert on the *observability contract*: that the checkout flow
emits log records with the expected ``action`` field and, critically, that
those records carry the ``request_id`` that was bound into the ContextVar
before the service was called.

The CheckoutService is exercised directly (no HTTP layer) using the same
``checkout_service`` fixture as the existing service tests.  The
``request_id_ctx`` ContextVar from ``apps.core.context`` is bound manually
to simulate what ``RequestIdMiddleware`` does in a real request.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

import pytest

from apps.cart.services import add_product_to_cart
from apps.core.context import bind_request_context, reset_request_context
from apps.core.idempotency import compute_request_hash


# ---------------------------------------------------------------------------
# Fixtures reused from the order conftest
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded_cart_for_logging(cart_factory, product_factory, address_factory, payment_method_factory):
    """Cart with one item, address, and payment method — ready for checkout."""
    cart = cart_factory()
    product = product_factory(price=Decimal("20.00"), stock=5)
    add_product_to_cart(cart, product, quantity=1)
    cart.refresh_from_db()
    address = address_factory(user_id=cart.user_id)
    pm = payment_method_factory()
    return cart, address, pm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _checkout(service, cart, address, pm, idempotency_key=None):
    key = idempotency_key or uuid.uuid4()
    request_hash = compute_request_hash({
        "cart_id": str(cart.id),
        "payment_method_id": str(pm.id),
        "address_id": str(address.id),
    })
    return service.checkout(
        cart_id=cart.id,
        payment_method_id=pm.id,
        address_id=address.id,
        idempotency_key=key,
        request_hash=request_hash,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_checkout_completed_log_contains_request_id(
    checkout_service,
    loaded_cart_for_logging,
    caplog,
    tenant,
):
    """checkout.completed log record carries the request_id bound before the call."""
    cart, address, pm = loaded_cart_for_logging
    test_rid = f"test-checkout-{uuid.uuid4().hex[:8]}"

    token = bind_request_context(request_id=test_rid, user_id=None)
    try:
        with caplog.at_level(logging.INFO, logger="apps.order.services"):
            _checkout(checkout_service, cart, address, pm)
    finally:
        reset_request_context(token)

    completed = [
        r for r in caplog.records
        if getattr(r, "action", None) == "checkout.completed"
    ]
    assert completed, "Expected at least one 'checkout.completed' log record"

    for record in completed:
        rid = getattr(record, "request_id", None)
        assert rid == test_rid, (
            f"checkout.completed record has request_id={rid!r}, expected {test_rid!r}"
        )
        assert getattr(record, "outcome", None) in ("success", "replay")


@pytest.mark.django_db(transaction=True)
def test_checkout_started_log_emitted(
    checkout_service,
    loaded_cart_for_logging,
    caplog,
    tenant,
):
    """checkout.started log is emitted at the beginning of the checkout flow."""
    cart, address, pm = loaded_cart_for_logging

    with caplog.at_level(logging.INFO, logger="apps.order.services"):
        _checkout(checkout_service, cart, address, pm)

    started = [r for r in caplog.records if getattr(r, "action", None) == "checkout.started"]
    assert started, "Expected a 'checkout.started' log record before checkout.completed"


@pytest.mark.django_db(transaction=True)
def test_checkout_lock_failed_log_and_metric(
    fake_redis,
    loaded_cart_for_logging,
    caplog,
    tenant,
    monkeypatch,
):
    """When the Redis lock is already held, checkout.lock_failed is logged."""
    from apps.core.exceptions import LockNotAcquired
    from apps.core.idempotency import IdempotencyManager
    from apps.order.services import CheckoutService

    cart, address, pm = loaded_cart_for_logging

    def _always_raise(_key, _ttl, *, client=None):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            raise LockNotAcquired("test lock held")
            yield  # noqa: unreachable

        return _cm()

    monkeypatch.setattr("apps.order.services.redis_lock", _always_raise)

    idem_manager = IdempotencyManager(redis_client=fake_redis, inprogress_ttl_s=60)
    service = CheckoutService(
        redis_client=fake_redis,
        payment_dispatcher=lambda pid: None,
        idem_manager=idem_manager,
        lock_ttl_ms=5000,
    )

    key = uuid.uuid4()
    request_hash = compute_request_hash({
        "cart_id": str(cart.id),
        "payment_method_id": str(pm.id),
        "address_id": str(address.id),
    })

    with caplog.at_level(logging.WARNING, logger="apps.order.services"):
        with pytest.raises(LockNotAcquired):
            service.checkout(
                cart_id=cart.id,
                payment_method_id=pm.id,
                address_id=address.id,
                idempotency_key=key,
                request_hash=request_hash,
            )

    lock_failed = [r for r in caplog.records if getattr(r, "action", None) == "checkout.lock_failed"]
    assert lock_failed, "Expected a 'checkout.lock_failed' WARNING log record"
