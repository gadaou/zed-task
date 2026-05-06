"""Unit tests for ``apps.cart.services``.

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

from apps.cart.models import CartItem
from apps.cart.services import (
    add_product_to_cart,
    remove_product_from_cart,
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
