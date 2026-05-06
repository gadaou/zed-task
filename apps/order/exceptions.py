"""Order / checkout domain exceptions.

Implements the checkout-related entries of the PROJECT_SPEC §2 error taxonomy:

    cart/not-found        — cart_id does not exist in this tenant.
    cart/empty            — cart has no items; cannot check out.
    cart/locked           — checkout already in progress (Redis lock held) or
                            cart is already checked out.
    cart/stale-version    — concurrent modification changed the cart between
                            the lock acquisition and the UPDATE.
    product/not-found     — a product referenced by a cart item no longer exists.
    product/out-of-stock  — a product does not have enough stock.
    address/not-found     — the supplied address_id does not belong to this user.
    payment/method-invalid — the supplied payment_method_id is not valid.

Design rules (mirrors apps/coupon/exceptions.py):
- All exceptions inherit from ``OrderDomainError`` for uniform ``except``
  coverage in the view layer.
- ``type`` matches the spec taxonomy verbatim.
- ``detail`` is safe-for-logs and carries no PII.
- ``extra`` kwargs allow machine-readable context (e.g. ``product_id``) to
  flow to the problem+json serializer without polluting the detail string.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


class OrderDomainError(Exception):
    """Base class for every checkout / order domain error."""

    type: str = "order/error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        self.detail = detail or self.__class__.__doc__ or ""
        self.extra = extra
        super().__init__(self.detail)


class CartNotFound(OrderDomainError):
    """The requested cart does not exist in this tenant."""

    type = "cart/not-found"


class CartEmpty(OrderDomainError):
    """The cart has no items and cannot be checked out."""

    type = "cart/empty"


class CartAlreadyCheckedOut(OrderDomainError):
    """The cart has already been checked out.

    Maps to ``cart/locked`` in the spec taxonomy because the client-visible
    semantics are "this cart is no longer available for checkout".
    """

    type = "cart/locked"


class CartStaleVersion(OrderDomainError):
    """The cart was modified concurrently; the checkout version guard failed.

    The client should re-read the cart and retry the checkout.
    """

    type = "cart/stale-version"


class ProductNotFound(OrderDomainError):
    """A product referenced by a cart item no longer exists in the catalog."""

    type = "product/not-found"

    def __init__(self, product_id: UUID | None = None, detail: str = "", **extra: Any) -> None:
        self.product_id = product_id
        super().__init__(
            detail or (
                f"product {product_id} not found in catalog"
                if product_id
                else "a product referenced by a cart item no longer exists"
            ),
            product_id=str(product_id) if product_id else None,
            **extra,
        )


class ProductOutOfStock(OrderDomainError):
    """A product does not have sufficient stock for the requested quantity."""

    type = "product/out-of-stock"

    def __init__(self, product_id: UUID | None = None, detail: str = "", **extra: Any) -> None:
        self.product_id = product_id
        super().__init__(
            detail or (
                f"product {product_id} is out of stock"
                if product_id
                else "a product in this cart is out of stock"
            ),
            product_id=str(product_id) if product_id else None,
            **extra,
        )


class AddressNotFound(OrderDomainError):
    """The supplied address does not exist or does not belong to this user."""

    type = "address/not-found"


class PaymentMethodInvalid(OrderDomainError):
    """The supplied payment method is not valid or does not belong to this tenant."""

    type = "payment/method-invalid"
