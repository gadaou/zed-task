"""Root URL configuration for cart_system.

Layout:

* ``/admin/``        Django admin (ops only).
* ``/healthz``       Liveness probe (no auth, no deps).
* ``/readyz``        Readiness probe (will check Postgres + Redis).
* ``/api/v1/...``    Versioned REST API. New apps mount their routers here.
* ``/api/schema/``   OpenAPI 3.1 schema (drf-spectacular).
* ``/api/docs/``     Swagger UI rendering of the schema.
* ``/api/redoc/``    Redoc rendering of the schema.

Versioning policy: PROJECT_SPEC §5.4 — breaking changes go to ``/v2/``;
``v1`` remains supported for at least 12 months past ``v2`` GA.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)


api_v1_patterns = [
    path("tenants/", include(("apps.tenant.urls", "tenant"), namespace="tenants")),
    path("carts/", include(("apps.cart.urls", "cart"), namespace="carts")),
    path("coupons/", include(("apps.coupon.urls", "coupon"), namespace="coupons")),
    path("payments/", include(("apps.payment.urls", "payment"), namespace="payments")),
    path("orders/", include(("apps.order.urls", "order"), namespace="orders")),
]


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include(("apps.core.urls", "core"), namespace="core")),
    path("api/v1/", include((api_v1_patterns, "v1"), namespace="v1")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
]
