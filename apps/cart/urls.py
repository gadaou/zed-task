"""URL configuration for the cart app.

Cart-level routes live here because the checkout action is an action on a cart
resource (``POST /v1/carts/{cart_id}/checkout``).  The view implementation is
in ``apps.order.views`` but the URL lives under the ``carts/`` prefix.
"""

from __future__ import annotations

from django.urls import path

from apps.order.views import CheckoutView

app_name = "cart"

urlpatterns = [
    # POST /api/v1/carts/{cart_id}/checkout/
    # PROJECT_SPEC §2 op 7 — checkout is an action on the cart resource.
    path(
        "<uuid:cart_id>/checkout/",
        CheckoutView.as_view(),
        name="checkout",
    ),
]
