"""Unit tests for ``apps.cart.services``.

Covers:
1. Core mutations — add/remove product, recalculate total.
2. Active-cart helpers — get_or_create, get_active_cart, lock_active_cart_for_update.
3. Selection helpers — set_cart_address, set_cart_payment_method.
4. DB-level constraint — uq_cart_one_active_per_tenant_user prevents two ACTIVE carts.
5. Cross-tenant isolation — same user can have an active cart in each tenant.

Covers the three operations defined in the Phase 3 task:
1. Adding a product (creates a CartItem with the right price snapshot).
2. Adding the same product again (updates quantity, does NOT duplicate rows).
3. Removing a product (deletes the line and recalculates the total).

Implementation notes
--------------------
* Each test is marked ``@pytest.mark.django_db(transaction=True)`` because
  ``select_for_update()`` requires a real transaction. Without
  ``transaction=True`` pytest-django wraps the test in a savepoint and the
  ``SELECT ... FOR UPDATE`` raises ``TransactionManagementError`` on
  PostgreSQL (silent on SQLite, but we want the same fixture to work on
  both).
* The tenant context is established by the ``tenant`` fixture in
  ``conftest.py``; every queryset issued in these tests is automatically
  scoped to that tenant.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import uuid

from apps.cart.models import Cart, CartItem
from apps.cart.services import (
    add_product_to_cart,
    get_active_cart,
    get_or_create_active_cart,
    lock_active_cart_for_update,
    remove_product_from_cart,
    set_cart_address,
    set_cart_payment_method,
)


@pytest.mark.django_db(transaction=True)
def test_add_product_creates_cart_item_with_price_snapshot(
    cart_factory, product_factory
) -> None:
    """First-time add: one CartItem, correct snapshot, total updated."""
    cart = cart_factory()
    product = product_factory(price=Decimal("12.50"))

    initial_version = cart.version

    updated = add_product_to_cart(cart, product, quantity=2)

    items = list(CartItem.objects.filter(cart=updated))
    assert len(items) == 1
    item = items[0]
    assert item.product_id == product.id
    assert item.quantity == 2
    # Price snapshot must match the catalog price at add-time, not be
    # recomputed later (PROJECT_SPEC §5.3).
    assert item.price_snapshot == Decimal("12.50")
    assert item.currency == product.currency

    assert updated.total_price == Decimal("25.00")
    assert updated.version == initial_version + 1


@pytest.mark.django_db(transaction=True)
def test_add_product_again_updates_quantity_not_row_count(
    cart_factory, product_factory
) -> None:
    """Re-adding the same product increments quantity on the existing row.

    Guards against the bug where a naive implementation creates a second
    CartItem and either violates ``uq_cartitem_cart_product`` or silently
    duplicates the line.
    """
    cart = cart_factory()
    product = product_factory(price=Decimal("4.00"))

    add_product_to_cart(cart, product, quantity=2)
    updated = add_product_to_cart(cart, product, quantity=3)

    items = list(CartItem.objects.filter(cart=updated))
    assert len(items) == 1
    assert items[0].quantity == 5
    assert updated.total_price == Decimal("20.00")


@pytest.mark.django_db(transaction=True)
def test_remove_product_deletes_item_and_recalculates_total(
    cart_factory, product_factory
) -> None:
    """Removing a product drops the line and zeroes the total."""
    cart = cart_factory()
    product = product_factory(price=Decimal("9.99"))

    add_product_to_cart(cart, product, quantity=1)
    assert CartItem.objects.filter(cart=cart).count() == 1

    updated = remove_product_from_cart(cart, product.id)

    assert CartItem.objects.filter(cart=updated).count() == 0
    assert updated.total_price == Decimal("0.00")


# ---------------------------------------------------------------------------
# New service helpers: get_or_create_active_cart, set_cart_address,
# set_cart_payment_method
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_get_or_create_active_cart_creates_new_cart(tenant) -> None:
    """First call creates an ACTIVE cart for the user."""
    user_id = uuid.uuid4()
    assert Cart.objects.filter(user_id=user_id).count() == 0

    cart = get_or_create_active_cart(user_id)

    assert cart.user_id == user_id
    assert cart.status == Cart.Status.ACTIVE
    assert Cart.objects.filter(user_id=user_id, status="ACTIVE").count() == 1


@pytest.mark.django_db(transaction=True)
def test_get_or_create_active_cart_returns_existing(tenant) -> None:
    """Subsequent calls return the same cart, not a duplicate."""
    user_id = uuid.uuid4()
    c1 = get_or_create_active_cart(user_id)
    c2 = get_or_create_active_cart(user_id)

    assert c1.id == c2.id
    assert Cart.objects.filter(user_id=user_id, status="ACTIVE").count() == 1


@pytest.mark.django_db(transaction=True)
def test_set_cart_address_updates_selection_and_bumps_version(
    cart_factory, tenant
) -> None:
    """set_cart_address stores the FK and increments version."""
    from apps.addresses.models import Address

    cart = cart_factory()
    initial_version = cart.version
    addr = Address.objects.create(
        user_id=cart.user_id, country="US", city="NY", details="5th Ave"
    )

    updated = set_cart_address(cart, addr)

    assert updated.selected_address_id == addr.id
    assert updated.version == initial_version + 1


@pytest.mark.django_db(transaction=True)
def test_set_cart_payment_method_updates_selection_and_bumps_version(
    cart_factory, tenant
) -> None:
    """set_cart_payment_method stores the FK and increments version."""
    from apps.payment.models import PaymentMethod

    cart = cart_factory()
    initial_version = cart.version
    pm = PaymentMethod.objects.create(gateway_slug="dummy_success")

    updated = set_cart_payment_method(cart, pm)

    assert updated.selected_payment_method_id == pm.id
    assert updated.version == initial_version + 1


# ---------------------------------------------------------------------------
# get_active_cart — explicit tenant_id lookup, no side-effects
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_get_active_cart_returns_none_when_no_cart_exists(tenant) -> None:
    """Returns None for a user who has no cart in the given tenant."""
    result = get_active_cart(tenant.id, uuid.uuid4())
    assert result is None


@pytest.mark.django_db(transaction=True)
def test_get_active_cart_returns_existing_cart(tenant) -> None:
    """Returns the ACTIVE cart row when one exists."""
    user_id = uuid.uuid4()
    cart = get_or_create_active_cart(user_id)

    result = get_active_cart(tenant.id, user_id)

    assert result is not None
    assert result.id == cart.id


@pytest.mark.django_db(transaction=True)
def test_get_active_cart_does_not_return_checked_out_cart(tenant) -> None:
    """A CHECKED_OUT cart is not visible to get_active_cart."""
    user_id = uuid.uuid4()
    cart = get_or_create_active_cart(user_id)
    Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.CHECKED_OUT)

    result = get_active_cart(tenant.id, user_id)

    assert result is None


# ---------------------------------------------------------------------------
# lock_active_cart_for_update — SELECT FOR UPDATE, raises when missing
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_lock_active_cart_for_update_raises_when_no_active_cart(tenant) -> None:
    """Raises CartNotFound when no ACTIVE cart exists."""
    from django.db import transaction as db_transaction
    from apps.order.exceptions import CartNotFound

    with pytest.raises(CartNotFound):
        with db_transaction.atomic():
            lock_active_cart_for_update(tenant.id, uuid.uuid4())


@pytest.mark.django_db(transaction=True)
def test_lock_active_cart_for_update_returns_locked_cart(tenant) -> None:
    """Returns the ACTIVE cart when it exists (lock only verifiable at DB level)."""
    from django.db import transaction as db_transaction

    user_id = uuid.uuid4()
    created = get_or_create_active_cart(user_id)

    with db_transaction.atomic():
        locked = lock_active_cart_for_update(tenant.id, user_id)

    assert locked.id == created.id
    assert locked.status == Cart.Status.ACTIVE


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_same_user_has_independent_active_cart_per_tenant() -> None:
    """Same user_id can hold one ACTIVE cart in each tenant — no collision."""
    from apps.tenant.models import Tenant
    from apps.tenant.context import tenant_context

    user_id = uuid.uuid4()

    tenant_a = Tenant.objects.create(name="Tenant A", domain=f"a-{uuid.uuid4().hex[:8]}.test")
    tenant_b = Tenant.objects.create(name="Tenant B", domain=f"b-{uuid.uuid4().hex[:8]}.test")

    with tenant_context(tenant_a):
        cart_a = get_or_create_active_cart(user_id)
    with tenant_context(tenant_b):
        cart_b = get_or_create_active_cart(user_id)

    assert cart_a.id != cart_b.id
    assert cart_a.tenant_id == tenant_a.id
    assert cart_b.tenant_id == tenant_b.id

    assert get_active_cart(tenant_a.id, user_id).id == cart_a.id
    assert get_active_cart(tenant_b.id, user_id).id == cart_b.id


@pytest.mark.django_db(transaction=True)
def test_tenant_a_cannot_see_tenant_b_cart_via_get_active_cart() -> None:
    """get_active_cart with tenant_a's ID must not return tenant_b's cart."""
    from apps.tenant.models import Tenant
    from apps.tenant.context import tenant_context

    user_id = uuid.uuid4()

    tenant_a = Tenant.objects.create(name="TA", domain=f"ta-{uuid.uuid4().hex[:8]}.test")
    tenant_b = Tenant.objects.create(name="TB", domain=f"tb-{uuid.uuid4().hex[:8]}.test")

    with tenant_context(tenant_b):
        get_or_create_active_cart(user_id)

    result = get_active_cart(tenant_a.id, user_id)
    assert result is None


# ---------------------------------------------------------------------------
# Partial unique constraint — prevents two ACTIVE carts per (tenant, user)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_duplicate_active_cart_rejected_by_db_constraint(tenant) -> None:
    """DB constraint uq_cart_one_active_per_tenant_user blocks a second INSERT."""
    from django.db import IntegrityError

    user_id = uuid.uuid4()
    Cart.objects.create(user_id=user_id, status=Cart.Status.ACTIVE, currency="USD")

    with pytest.raises(IntegrityError):
        Cart.objects.create(user_id=user_id, status=Cart.Status.ACTIVE, currency="USD")


@pytest.mark.django_db(transaction=True)
def test_checked_out_cart_does_not_block_new_active_cart(tenant) -> None:
    """A CHECKED_OUT cart for the same user does NOT trigger the constraint."""
    user_id = uuid.uuid4()
    Cart.objects.create(user_id=user_id, status=Cart.Status.CHECKED_OUT, currency="USD")

    new_cart = Cart.objects.create(user_id=user_id, status=Cart.Status.ACTIVE, currency="USD")

    assert new_cart.pk is not None
