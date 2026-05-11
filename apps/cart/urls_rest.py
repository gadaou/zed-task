"""URL configuration for the canonical RESTful cart endpoints.

Routes are mounted under ``/api/v1/cart/`` (singular) alongside
``urls_actions.py``.  These are the **preferred** API surface going forward;
the action-style routes in ``urls_actions.py`` are kept for backwards
compatibility but are marked deprecated in the OpenAPI schema.

Per PROJECT_SPEC §5.4: "Resource-oriented URLs. /v1/carts/{id}/items/{id}.
No verbs in paths except for true actions (/checkout)."

Endpoint layout
---------------
POST   /api/v1/cart/items/                     CartItemsView
DELETE /api/v1/cart/items/{product_id}/        CartItemDetailView
POST   /api/v1/cart/coupons/                   CartCouponsView
DELETE /api/v1/cart/coupons/{coupon_id}/       CartCouponDetailView
PUT    /api/v1/cart/address/                   CartAddressView
PUT    /api/v1/cart/payment-method/            CartPaymentMethodView
PUT    /api/v1/cart/business-details/          CartBusinessDetailsView

Note: GET /api/v1/cart/ and POST /api/v1/cart/checkout/ are already wired
in urls_actions.py and do not need aliases — they have no verb in the path.
"""

from __future__ import annotations

from django.urls import path

from apps.cart.views import (
    CartAddressView,
    CartBusinessDetailsView,
    CartCouponDetailView,
    CartCouponsView,
    CartItemDetailView,
    CartItemsView,
    CartPaymentMethodView,
)

app_name = "cart_rest"

urlpatterns = [
    # POST /api/v1/cart/items/
    path("items/", CartItemsView.as_view(), name="items"),
    # DELETE /api/v1/cart/items/{product_id}/
    path("items/<uuid:product_id>/", CartItemDetailView.as_view(), name="item-detail"),
    # POST /api/v1/cart/coupons/
    path("coupons/", CartCouponsView.as_view(), name="coupons"),
    # DELETE /api/v1/cart/coupons/{coupon_id}/
    path(
        "coupons/<uuid:coupon_id>/",
        CartCouponDetailView.as_view(),
        name="coupon-detail",
    ),
    # PUT /api/v1/cart/address/
    path("address/", CartAddressView.as_view(), name="address"),
    # PUT /api/v1/cart/payment-method/
    path("payment-method/", CartPaymentMethodView.as_view(), name="payment-method"),
    # PUT /api/v1/cart/business-details/
    path(
        "business-details/", CartBusinessDetailsView.as_view(), name="business-details"
    ),
]
