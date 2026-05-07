"""Cart services — business logic for the cart aggregate.

Implements the operations from PROJECT_SPEC §2 (rows 1–2) and the principles
from §4.1 (no business logic in views), §4.3 (transactions for critical
writes), and §3.4 (row locks + optimistic version on Cart).

Public API
----------
- ``get_or_create_active_cart(user_id)``
    Return (or create) the single ACTIVE cart for the current tenant context.
    Used by all customer-facing action endpoints (no ``cart_id`` required).
- ``get_active_cart(tenant_id, user_id)``
    Read-only lookup — returns the ACTIVE cart or ``None``. Accepts an
    explicit ``tenant_id`` so it can be called outside the implicit
    thread-local context (e.g. Celery workers, management commands).
- ``lock_active_cart_for_update(tenant_id, user_id)``
    Like ``get_active_cart`` but acquires a ``SELECT FOR UPDATE`` row lock.
    Raises ``CartNotFound`` if no ACTIVE cart exists.  Caller must be inside
    ``transaction.atomic()``.
- ``add_product_to_cart(cart, product, quantity)``
    Add (or top-up) a product line on a cart. Captures ``price_snapshot`` at
    add-time per §5.3 — the live catalog price may drift; checkout will
    re-validate. If a line for the same product already exists on this cart
    the ``uq_cartitem_cart_product`` unique constraint forces us into the
    update path (quantity += new), avoiding duplicate rows.
- ``remove_product_from_cart(cart, product_id)``
    Remove a product line entirely. Idempotent — removing a product that is
    not on the cart is a no-op (matches §2 "Remove product = idempotent").
- ``recalculate_cart(cart)``
    Re-derive ``Cart.total_price`` from the line items and bump
    ``Cart.version``. Called internally after every mutation; also exposed for
    callers that need to force a re-derivation (e.g. after a coupon apply in
    a future iteration).

Concurrency model
-----------------
Every mutating function:
1. Wraps its body in ``transaction.atomic()`` so partial failures never
   leave a half-updated cart.
2. Re-fetches the cart row with ``select_for_update()`` to serialize
   concurrent mutations on the same cart (§3.4 — DB row lock for "rows
   whose state we're about to mutate based on its current value").
3. Bumps ``Cart.version`` via an ``F`` expression so the increment happens
   in SQL atomically with the rest of the write.

The version bump is the optimistic-concurrency token defined in §3.4.
A future iteration will add ``WHERE version = :v`` guards on the views
that read-modify-write so a stale client gets ``409 cart/stale-version``
instead of silently overwriting a concurrent change.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Coalesce

from apps.cart.models import Cart, CartItem
from apps.catalog.models import Product
from apps.core.cache import schedule_cart_cache_invalidation
from apps.core.logging import log_event

# TYPE_CHECKING guards avoid circular imports at runtime; the models are used
# only in type annotations so this is safe.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.addresses.models import Address
    from apps.payment.models import PaymentMethod

logger = logging.getLogger(__name__)


def get_or_create_active_cart(user_id: UUID) -> Cart:
    """Return the single ACTIVE cart for ``user_id`` in the current tenant.

    If no active cart exists, one is created.

    **Tenant isolation mechanism**

    ``Cart.objects`` is a ``TenantAwareManager``. Its ``get_queryset()`` calls
    ``get_current_tenant()`` and injects ``WHERE tenant_id = <tenant>`` into
    every query before it hits the database.  Both the GET and the INSERT legs
    of the underlying ``get_or_create`` call are therefore automatically scoped
    to the tenant that ``TenantMiddleware`` placed in the ``ContextVar`` for
    the current request.  Calling this function without an active tenant context
    raises ``TenantContextMissing`` before any SQL is issued.

    **Concurrency safety**

    The ``uq_cart_one_active_per_tenant_user`` partial unique index (migration
    0005) makes the GET→INSERT race impossible: if two concurrent requests both
    pass the GET phase, only one INSERT succeeds; the loser receives
    ``IntegrityError`` and Django's ``get_or_create`` retries with another GET,
    returning the winner's cart.

    Args:
        user_id: UUID of the customer whose active cart is wanted.

    Returns:
        The existing or newly-created active Cart, scoped to the current tenant.
    """
    cart, _ = Cart.objects.get_or_create(
        user_id=user_id,
        status=Cart.Status.ACTIVE,
        defaults={"currency": "USD"},
    )
    return cart


def get_active_cart(tenant_id: UUID, user_id: UUID) -> Cart | None:
    """Return the ACTIVE cart for ``(tenant_id, user_id)`` or ``None``.

    Unlike ``get_or_create_active_cart`` this helper takes an **explicit**
    ``tenant_id`` argument so it can safely be called from outside the
    request/response cycle (Celery workers, management commands) where the
    thread-local tenant context set by ``TenantMiddleware`` may not be
    present.

    Args:
        tenant_id: UUID of the tenant that owns the cart.
        user_id:   UUID of the customer.

    Returns:
        The ACTIVE Cart, or ``None`` if the customer has no active cart in
        that tenant.
    """
    return (
        Cart.objects.all_tenants()
        .filter(tenant_id=tenant_id, user_id=user_id, status=Cart.Status.ACTIVE)
        .first()
    )


def lock_active_cart_for_update(tenant_id: UUID, user_id: UUID) -> Cart:
    """Return the ACTIVE cart with a ``SELECT FOR UPDATE`` row lock.

    Provides a safe way for service-layer code that needs to mutate the cart
    and then perform additional reads in the same transaction to prevent
    concurrent modifications.

    **Must be called inside a ``transaction.atomic()`` block.**

    Args:
        tenant_id: UUID of the tenant that owns the cart.
        user_id:   UUID of the customer.

    Returns:
        The ACTIVE Cart, locked for the duration of the enclosing transaction.

    Raises:
        CartNotFound: If no ACTIVE cart exists for the given tenant/user pair.
    """
    from apps.order.exceptions import CartNotFound

    cart = (
        Cart.objects.all_tenants()
        .select_for_update()
        .filter(tenant_id=tenant_id, user_id=user_id, status=Cart.Status.ACTIVE)
        .first()
    )
    if cart is None:
        raise CartNotFound(f"No active cart for user {user_id} in tenant {tenant_id}")
    return cart


@transaction.atomic
def set_cart_address(cart: Cart, address: "Address") -> Cart:
    """Set ``address`` as the selected shipping address on ``cart``.

    Acquires a row lock on the cart, updates ``selected_address``, and bumps
    ``version`` so concurrent mutations are detected (PROJECT_SPEC §3.4).

    Args:
        cart:    The Cart aggregate to mutate.
        address: An Address instance belonging to the same tenant and user.

    Returns:
        The same Cart instance with ``selected_address`` and ``version``
        refreshed from the database.
    """
    t0 = time.monotonic()
    try:
        locked = Cart.objects.select_for_update().get(pk=cart.pk)
        locked.selected_address = address
        locked.version = F("version") + 1
        locked.save(update_fields=["selected_address", "version", "updated_at"])
        locked.refresh_from_db(fields=["selected_address_id", "version"])
        schedule_cart_cache_invalidation(locked.tenant_id, locked.user_id)
    except Exception:
        log_event(
            logger,
            "cart.set_address",
            level=logging.ERROR,
            outcome="failed",
            cart_id=str(cart.pk),
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        raise
    log_event(
        logger,
        "cart.set_address",
        outcome="success",
        cart_id=str(cart.pk),
        duration_ms=round((time.monotonic() - t0) * 1000),
    )
    return locked


@transaction.atomic
def set_cart_payment_method(cart: Cart, payment_method: "PaymentMethod") -> Cart:
    """Set ``payment_method`` as the selected payment method on ``cart``.

    Acquires a row lock on the cart, updates ``selected_payment_method``, and
    bumps ``version`` (PROJECT_SPEC §3.4).

    Args:
        cart:           The Cart aggregate to mutate.
        payment_method: A PaymentMethod instance belonging to the same tenant.

    Returns:
        The same Cart instance with ``selected_payment_method`` and ``version``
        refreshed from the database.
    """
    t0 = time.monotonic()
    try:
        locked = Cart.objects.select_for_update().get(pk=cart.pk)
        locked.selected_payment_method = payment_method
        locked.version = F("version") + 1
        locked.save(update_fields=["selected_payment_method", "version", "updated_at"])
        locked.refresh_from_db(fields=["selected_payment_method_id", "version"])
        schedule_cart_cache_invalidation(locked.tenant_id, locked.user_id)
    except Exception:
        log_event(
            logger,
            "cart.set_payment_method",
            level=logging.ERROR,
            outcome="failed",
            cart_id=str(cart.pk),
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        raise
    log_event(
        logger,
        "cart.set_payment_method",
        outcome="success",
        cart_id=str(cart.pk),
        duration_ms=round((time.monotonic() - t0) * 1000),
    )
    return locked


@transaction.atomic
def add_product_to_cart(cart: Cart, product: Product, quantity: int) -> Cart:
    """Add ``quantity`` of ``product`` to ``cart``.

    If a CartItem already exists for ``(cart, product.id)`` the existing
    row's ``quantity`` is incremented by ``quantity`` (the unique constraint
    ``uq_cartitem_cart_product`` guarantees at most one row per pair).
    Otherwise a new CartItem is created with ``price_snapshot`` and
    ``currency`` copied from the catalog at this exact moment (§5.3).

    Args:
        cart: The Cart aggregate to mutate. Must already be persisted; the
            row is re-fetched with ``select_for_update`` inside this call.
        product: The catalog Product being added. Its ``id``, ``price``, and
            ``currency`` are read; the Product row itself is not mutated or
            locked here (stock decrement happens at checkout, §5.3).
        quantity: Strictly positive integer count to add.

    Returns:
        The same Cart instance with ``total_price`` and ``version``
        refreshed from the database.

    Raises:
        ValueError: If ``quantity < 1`` — DB CHECK ``ck_cartitem_qty_pos``
            would also reject it, but failing fast in Python yields a
            clearer error and avoids burning a transaction.
        Cart.DoesNotExist: If the cart was deleted between the caller
            obtaining the in-memory instance and this function acquiring
            the row lock.
    """
    if quantity < 1:
        raise ValueError("quantity must be >= 1")

    t0 = time.monotonic()
    try:
        locked = Cart.objects.select_for_update().get(pk=cart.pk)

        item, created = CartItem.objects.get_or_create(
            cart=locked,
            product_id=product.id,
            defaults={
                "quantity": quantity,
                "price_snapshot": product.price,
                "currency": product.currency,
            },
        )
        if not created:
            # F() expression so the increment is computed in SQL — avoids the
            # classic read-modify-write race even though we already hold the
            # cart row lock (defense in depth).
            item.quantity = F("quantity") + quantity
            item.save(update_fields=["quantity", "updated_at"])
            item.refresh_from_db(fields=["quantity"])

        result = recalculate_cart(locked)
        schedule_cart_cache_invalidation(locked.tenant_id, locked.user_id)
    except Exception:
        log_event(
            logger,
            "cart.add_product",
            level=logging.ERROR,
            outcome="failed",
            cart_id=str(cart.pk),
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        raise
    log_event(
        logger,
        "cart.add_product",
        outcome="success",
        cart_id=str(cart.pk),
        duration_ms=round((time.monotonic() - t0) * 1000),
    )
    return result


@transaction.atomic
def remove_product_from_cart(cart: Cart, product_id: UUID) -> Cart:
    """Remove the line for ``product_id`` from ``cart``.

    Idempotent: if no line exists for that product, the function still
    succeeds and recalculates the cart (which is a no-op delta on the
    total but still bumps ``version`` for consistency with every other
    mutating call).

    Args:
        cart: The Cart aggregate to mutate.
        product_id: UUID of the product whose line should be removed.

    Returns:
        The same Cart instance with ``total_price`` and ``version``
        refreshed from the database.
    """
    t0 = time.monotonic()
    try:
        locked = Cart.objects.select_for_update().get(pk=cart.pk)
        CartItem.objects.filter(cart=locked, product_id=product_id).delete()
        result = recalculate_cart(locked)
        schedule_cart_cache_invalidation(locked.tenant_id, locked.user_id)
    except Exception:
        log_event(
            logger,
            "cart.remove_product",
            level=logging.ERROR,
            outcome="failed",
            cart_id=str(cart.pk),
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        raise
    log_event(
        logger,
        "cart.remove_product",
        outcome="success",
        cart_id=str(cart.pk),
        duration_ms=round((time.monotonic() - t0) * 1000),
    )
    return result


def recalculate_cart(cart: Cart) -> Cart:
    """Re-derive denormalised totals on ``cart`` and bump ``version``.

    Three denormalised fields are kept in sync by this function:

    * ``total_price`` — the sum of (quantity × price_snapshot) over CartItems.
    * ``discount_amount`` — the sum of every applied coupon's
      ``CartCoupon.discount_amount`` snapshot (PROJECT_SPEC §2 ops 3–4).
    * ``total_after_discount`` — ``total_price - discount_amount`` clamped at
      zero so a fixed coupon larger than the subtotal cannot push the
      payable amount negative; the DB CHECK ``ck_cart_total_after_discount_nonneg``
      is the schema-level safety net for the same invariant.

    Every mutating service (add/remove product, apply/remove coupon) calls
    this as its final step so a cart read never has to re-aggregate.

    The function does NOT open its own ``transaction.atomic`` block — its
    public callers above already do so and call this as the final step.
    Calling it standalone is also safe: the single ``UPDATE`` it issues is
    atomic on its own.

    Args:
        cart: A Cart instance whose ``items`` and ``applied_coupons`` reverse
            FKs are queryable. Must already be persisted. Caller is
            responsible for holding any necessary row lock.

    Returns:
        The same Cart instance with ``total_price``, ``discount_amount``,
        ``total_after_discount``, and ``version`` refreshed from the database.
    """
    items_total = cart.items.aggregate(
        total=Coalesce(Sum(F("quantity") * F("price_snapshot")), Decimal("0.00"))
    )["total"]
    coupons_total = cart.applied_coupons.aggregate(
        total=Coalesce(Sum("discount_amount"), Decimal("0.00"))
    )["total"]

    # Clamp at zero — the schema CHECK rejects negatives, and a fixed coupon
    # bigger than the subtotal is a legitimate UX state (the customer pays
    # zero, the leftover discount is forfeited).
    payable = items_total - coupons_total
    if payable < 0:
        payable = Decimal("0.00")

    cart.total_price = items_total
    cart.discount_amount = coupons_total
    cart.total_after_discount = payable
    cart.version = F("version") + 1
    cart.save(
        update_fields=[
            "total_price",
            "discount_amount",
            "total_after_discount",
            "version",
            "updated_at",
        ]
    )
    cart.refresh_from_db(
        fields=[
            "total_price",
            "discount_amount",
            "total_after_discount",
            "version",
        ]
    )
    return cart
