"""URL configuration for the cart action-style endpoints.

Routes are mounted under ``/api/v1/cart/`` (singular) from ``cart_system/urls.py``.
This module is separate from ``apps/cart/urls.py`` which hosts the existing
resource-oriented ``<uuid:cart_id>/checkout/`` route under the ``carts/``
(plural) mount — both co-exist without collision.

Endpoint layout
---------------
GET  /api/v1/cart/                      CartReadView
POST /api/v1/cart/add-product/          AddProductView
POST /api/v1/cart/remove-product/       RemoveProductView
POST /api/v1/cart/add-coupon/           ApplyCouponView
POST /api/v1/cart/remove-coupon/        RemoveCouponView
POST /api/v1/cart/add-address/          AddAddressView
POST /api/v1/cart/add-payment-method/   AddPaymentMethodView
POST /api/v1/cart/checkout/             CartCheckoutView
"""

from __future__ import annotations

from django.urls import path

from apps.cart.views import (
    AddAddressView,
    AddPaymentMethodView,
    AddProductView,
    ApplyCouponView,
    CartCheckoutView,
    CartReadView,
    RemoveCouponView,
    RemoveProductView,
)

app_name = "cart_actions"

urlpatterns = [
    # GET /api/v1/cart/  — retrieve (or auto-create) the active cart
    path("", CartReadView.as_view(), name="read"),

    # POST /api/v1/cart/add-product/
    path("add-product/", AddProductView.as_view(), name="add-product"),

    # POST /api/v1/cart/remove-product/
    path("remove-product/", RemoveProductView.as_view(), name="remove-product"),

    # POST /api/v1/cart/add-coupon/
    path("add-coupon/", ApplyCouponView.as_view(), name="add-coupon"),

    # POST /api/v1/cart/remove-coupon/
    path("remove-coupon/", RemoveCouponView.as_view(), name="remove-coupon"),

    # POST /api/v1/cart/add-address/
    path("add-address/", AddAddressView.as_view(), name="add-address"),

    # POST /api/v1/cart/add-payment-method/
    path("add-payment-method/", AddPaymentMethodView.as_view(), name="add-payment-method"),

    # POST /api/v1/cart/checkout/
    path("checkout/", CartCheckoutView.as_view(), name="checkout"),
]
