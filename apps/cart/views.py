"""Cart views — DRF endpoints for cart and cart-item resources.

Per PROJECT_SPEC §4.1, views are thin: parse, validate (serializers),
dispatch to ``services``, serialize the result.

Endpoint set (action-style, user resolved from ``X-User-Id`` header):

    GET  /api/v1/cart                  — retrieve (or auto-create) the active cart
    POST /api/v1/cart/add-product      — add a product to the cart
    POST /api/v1/cart/remove-product   — remove a product from the cart
    POST /api/v1/cart/add-coupon       — apply a coupon code
    POST /api/v1/cart/remove-coupon    — remove an applied coupon
    POST /api/v1/cart/add-address      — create + select a shipping address
    POST /api/v1/cart/add-payment-method — create + select a payment method
    POST /api/v1/cart/checkout         — initiate checkout (requires Idempotency-Key)

Tenant context is provided by ``TenantMiddleware`` via ``request.tenant`` and
the ``ContextVar`` (PROJECT_SPEC §4.2).

User identity is resolved from the ``X-User-Id`` header (a UUID).  This is
the interim contract until bearer-token authentication lands (ADR-NOTE:
PROJECT_SPEC §4.3 — auth is delegated to the API gateway; the gateway will
inject the user identity as a header once bearer-token validation is wired).

Error shape: RFC 7807 problem+json (PROJECT_SPEC §6.4).
All helpers live in ``apps.core.responses`` so the shape is consistent with
the checkout endpoint in ``apps.order.views``.
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.addresses.services import add_address
from apps.core.cache import get_cart_cache, set_cart_cache
from apps.cart.serializers import (
    AddAddressSerializer,
    AddPaymentMethodSerializer,
    AddProductSerializer,
    ApplyCouponSerializer,
    CartCheckoutSerializer,
    CartReadSerializer,
    RemoveCouponSerializer,
    RemoveProductSerializer,
    SetBusinessDetailsSerializer,
)
from apps.cart.services import (
    add_product_to_cart,
    get_or_create_active_cart,
    remove_product_from_cart,
    set_business_details,
    set_cart_address,
    set_cart_payment_method,
)
from apps.catalog.models import Product
from apps.core.exceptions import (
    IdempotencyConflict,
    IdempotencyInProgress,
    LockNotAcquired,
)
from apps.core.idempotency import compute_request_hash
from apps.core.openapi import (
    IDEMPOTENCY_KEY_HEADER,
    TENANT_DOMAIN_HEADER,
    USER_ID_HEADER,
    X_REQUEST_ID_HEADER,
    add_address_examples,
    add_payment_method_examples,
    add_product_examples,
    apply_coupon_examples,
    b2b_request_examples,
    cart_read_response_examples,
    checkout_request_examples,
    checkout_response_examples,
    problem_response,
    remove_coupon_examples,
    remove_product_examples,
)
from apps.core.throttling import TenantUserScopedThrottle
from apps.core.responses import map_exception, problem, validation_problem
from apps.coupon.exceptions import CouponDomainError
from apps.coupon.services import CouponService
from apps.order.exceptions import (
    OrderDomainError,
)
from apps.order.serializers import CheckoutResponseSerializer
from apps.order.services import CheckoutService
from apps.payment.services import add_payment_method

logger = logging.getLogger(__name__)

_COUPON_SERVICE = CouponService()


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _resolve_user_id(request: Request) -> UUID | None:
    """Return the UUID from the ``X-User-Id`` header or ``None`` if absent/invalid."""
    raw = request.META.get("HTTP_X_USER_ID", "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _user_id_required(request: Request) -> "tuple[UUID, None] | tuple[None, Response]":
    """Resolve ``X-User-Id``; return ``(user_id, None)`` or ``(None, error_response)``."""
    user_id = _resolve_user_id(request)
    if user_id is None:
        return None, problem(
            "validation/user-id-required",
            "X-User-Id header is required",
            400,
            "Supply a valid UUID in the X-User-Id header to identify the customer.",
        )
    return user_id, None


def _cart_response(cart) -> Response:
    """Prefetch related items and return a full ``CartReadSerializer`` response."""
    # Prefetch before serialization so ``get_items`` / ``get_applied_coupons``
    # don't issue N+1 queries.

    cart.items.prefetch_related()
    cart._prefetched_objects_cache = {}  # reset any stale cache
    data = CartReadSerializer(cart).data
    return Response(data, status=200)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


_CART_COMMON_ERRORS = {
    400: problem_response(
        400,
        "validation/user-id-required",
        "X-User-Id header is required or invalid",
        "Supply a valid UUID in the X-User-Id header.",
        description=(
            "Returned when `X-User-Id` is absent or not a valid UUID, or when "
            "the request body fails serializer validation."
        ),
    ),
    422: problem_response(
        422,
        "product/not-found",
        "Business rule violation",
        "product cccccccc-… not found",
        description=(
            "Returned when a domain invariant is violated (e.g. product not "
            "found, coupon already applied, coupon expired)."
        ),
    ),
}

_RATE_LIMIT_RESPONSE = problem_response(
    429,
    "rate-limit/exceeded",
    "Too many requests",
    "Rate limit exceeded — retry after the window resets.",
    description=(
        "Returned when the per-tenant/per-user request rate exceeds the "
        "configured limit for this endpoint. Retry after the fixed window "
        "resets (typically 1 minute)."
    ),
)


class CartReadView(APIView):
    """``GET /api/v1/cart``

    Return the caller's active cart within the current tenant, creating one if
    it does not yet exist.  No request body required.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_read",
        summary="Get the caller's active cart",
        description=(
            "Returns (or auto-creates) the single **ACTIVE** cart for the "
            "authenticated customer within the current tenant.\n\n"
            "No `cart_id` is required — the active cart is resolved from "
            "`X-Tenant-Domain` + `X-User-Id`. A new cart is transparently "
            "created on the first call if none exists."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="The caller's active cart.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=cart_read_response_examples(),
    )
    def get(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        from apps.tenant.context import get_current_tenant

        tenant = get_current_tenant()
        if tenant is not None:
            cached = get_cart_cache(tenant.id, user_id)
            if cached is not None:
                return Response(cached, status=200)

        cart = get_or_create_active_cart(user_id)
        data = CartReadSerializer(cart).data
        if tenant is not None:
            set_cart_cache(tenant.id, user_id, data)
        return Response(data, status=200)


class AddProductView(APIView):
    """``POST /api/v1/cart/add-product``

    Add (or top-up) a product line on the caller's active cart.

    Request body: ``{"product_id": "<uuid>", "quantity": <int>}``
    """

    permission_classes = [AllowAny]
    throttle_classes = [TenantUserScopedThrottle]
    throttle_scope = "add_product"

    @extend_schema(
        operation_id="cart_add_product",
        summary="[Legacy] Add a product to the active cart",
        deprecated=True,
        description=(
            "Adds `quantity` units of `product_id` to the caller's active cart, "
            "creating the cart if it does not yet exist.\n\n"
            "If the product is already in the cart the existing quantity is "
            "**incremented** — no duplicate line is created.\n\n"
            "Rate limit: **60 requests / minute** per tenant + user."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=AddProductSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart after product was added.",
            ),
            **_CART_COMMON_ERRORS,
            404: problem_response(
                404,
                "product/not-found",
                "Product not found",
                "product cccccccc-… not found in this tenant's catalog",
                description=(
                    "Returned when `product_id` does not exist within the current tenant."
                ),
            ),
            429: _RATE_LIMIT_RESPONSE,
        },
        examples=add_product_examples(),
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = AddProductSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        data = serializer.validated_data
        product_id: UUID = data["product_id"]
        quantity: int = data["quantity"]

        # TenantAwareManager.get_queryset() filters by tenant=get_current_tenant()
        # before this .get() runs, so the lookup is already scoped to the active
        # tenant. A product_id that belongs to another tenant raises DoesNotExist
        # here — not a silent data leak — giving tenant isolation at the DB layer.
        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return problem(
                "product/not-found",
                "Product not found",
                404,
                f"product {product_id} not found in this tenant's catalog",
            )

        cart = get_or_create_active_cart(user_id)

        try:
            cart = add_product_to_cart(cart, product, quantity)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class RemoveProductView(APIView):
    """``POST /api/v1/cart/remove-product``

    Remove the line for a product from the caller's active cart.  Idempotent.

    Request body: ``{"product_id": "<uuid>"}``
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_remove_product",
        summary="[Legacy] Remove a product from the active cart",
        deprecated=True,
        description=(
            "Removes the line for `product_id` from the caller's active cart.\n\n"
            "**Idempotent** — if the product is not in the cart the call "
            "succeeds silently and returns the unchanged cart."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=RemoveProductSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart after product was removed.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=remove_product_examples(),
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = RemoveProductSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        product_id: UUID = serializer.validated_data["product_id"]
        cart = get_or_create_active_cart(user_id)

        try:
            cart = remove_product_from_cart(cart, product_id)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class ApplyCouponView(APIView):
    """``POST /api/v1/cart/add-coupon``

    Apply a coupon code to the caller's active cart.

    Request body: ``{"code": "<coupon-code>"}``
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_apply_coupon",
        summary="[Legacy] Apply a coupon to the active cart",
        deprecated=True,
        description=(
            "Validates and applies a coupon code to the caller's active cart. "
            "The discount is computed immediately and stored as a snapshot on "
            "`CartCoupon.discount_amount`.\n\n"
            "Returns `422` when the coupon is expired, usage-limit-reached, or "
            "already applied to this cart."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=ApplyCouponSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart with coupon applied.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=apply_coupon_examples(),
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = ApplyCouponSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        code: str = serializer.validated_data["code"]
        cart = get_or_create_active_cart(user_id)

        try:
            cart = _COUPON_SERVICE.apply_coupon_to_cart(cart, code)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class RemoveCouponView(APIView):
    """``POST /api/v1/cart/remove-coupon``

    Remove a coupon from the caller's active cart by coupon UUID.  Idempotent.

    Request body: ``{"coupon_id": "<uuid>"}``
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_remove_coupon",
        summary="[Legacy] Remove a coupon from the active cart",
        deprecated=True,
        description=(
            "Removes the applied coupon identified by `coupon_id` from the "
            "caller's active cart and recalculates the totals.\n\n"
            "**Idempotent** — removing a coupon that is not applied is a no-op."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=RemoveCouponSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart after coupon was removed.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=remove_coupon_examples(),
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = RemoveCouponSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        coupon_id: UUID = serializer.validated_data["coupon_id"]
        cart = get_or_create_active_cart(user_id)

        try:
            cart = _COUPON_SERVICE.remove_coupon_from_cart(cart, coupon_id)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class AddAddressView(APIView):
    """``POST /api/v1/cart/add-address``

    Create a new shipping address for the caller and set it as the cart's
    selected address.  The address is also persisted to the customer's address
    book (``Address`` model).

    Request body::

        {
            "country":    "US",
            "city":       "Springfield",
            "details":    "742 Evergreen Terrace",
            "label":      "home",       // optional
            "is_default": true          // optional, default false
        }
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_add_address",
        summary="[Legacy] Add a shipping address and select it on the active cart",
        deprecated=True,
        description=(
            "Creates a new `Address` record for the customer and immediately "
            "sets it as the `selected_address` on their active cart.\n\n"
            "This address is required before calling `POST /cart/checkout/`."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=AddAddressSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart with the new address selected.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=add_address_examples(),
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = AddAddressSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        data = serializer.validated_data
        cart = get_or_create_active_cart(user_id)

        try:
            address = add_address(
                user_id=user_id,
                country=data["country"],
                city=data["city"],
                details=data["details"],
                label=data.get("label", ""),
                is_default=data.get("is_default", False),
            )
            cart = set_cart_address(cart, address)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class AddPaymentMethodView(APIView):
    """``POST /api/v1/cart/add-payment-method``

    Create a new payment method and set it as the cart's selected payment
    method.

    Request body: ``{"gateway_slug": "dummy_success"}``

    The ``gateway_slug`` must name a registered gateway; unknown slugs return
    ``422 payment/gateway-unavailable``.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_add_payment_method",
        summary="[Legacy] Add a payment method and select it on the active cart",
        deprecated=True,
        description=(
            "Creates a new `PaymentMethod` record linked to the given gateway "
            "and immediately sets it as the `selected_payment_method` on the "
            "caller's active cart.\n\n"
            "This payment method is required before calling `POST /cart/checkout/`. "
            "Returns `422` if `gateway_slug` is not a registered gateway."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=AddPaymentMethodSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart with the new payment method selected.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=add_payment_method_examples(),
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = AddPaymentMethodSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        gateway_slug: str = serializer.validated_data["gateway_slug"]
        cart = get_or_create_active_cart(user_id)

        try:
            payment_method = add_payment_method(gateway_slug=gateway_slug)
            cart = set_cart_payment_method(cart, payment_method)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class SetBusinessDetailsView(APIView):
    """``POST /api/v1/cart/set-business-details``

    Set B2B buyer-metadata on the caller's active cart.  The fields are
    persisted to the cart and returned immediately in the response, so a
    subsequent ``GET /api/v1/cart/`` reflects them.  At checkout time the
    checkout service snapshots these fields onto the Order.

    Request body (all fields optional, at least one must be non-empty)::

        {
            "company_name":              "Acme Corp Ltd",      // optional
            "tax_number":                "GB123456789",        // optional
            "purchase_order_reference":  "PO-2026-00042"      // optional
        }
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_set_business_details",
        summary="[Legacy] Set B2B business details on the active cart",
        deprecated=True,
        description=(
            "Stores company name, tax/VAT number, and/or purchase-order "
            "reference on the caller's active cart.\n\n"
            "All three fields are optional individually, but at least one "
            "must be non-empty. They are visible in `GET /api/v1/cart/` "
            "immediately and are snapshotted onto the Order at checkout time.\n\n"
            "Calling this endpoint is idempotent — repeated calls overwrite "
            "the previous values."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=SetBusinessDetailsSerializer,
        examples=b2b_request_examples(),
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart with B2B details applied.",
            ),
            **_CART_COMMON_ERRORS,
        },
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = SetBusinessDetailsSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        data = serializer.validated_data
        cart = get_or_create_active_cart(user_id)

        try:
            cart = set_business_details(
                cart,
                company_name=data.get("company_name", ""),
                tax_number=data.get("tax_number", ""),
                purchase_order_reference=data.get("purchase_order_reference", ""),
            )
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class CartCheckoutView(APIView):
    """``POST /api/v1/cart/checkout``

    Initiate checkout for the caller's active cart.  Uses the cart's
    ``selected_address`` and ``selected_payment_method`` (set by earlier
    ``add-address`` / ``add-payment-method`` calls).

    Requires:
    - Header ``Idempotency-Key: <uuid>`` (PROJECT_SPEC §4.5).
    - Cart must have ``selected_address`` and ``selected_payment_method`` set
      (422 ``cart/checkout-incomplete`` if either is missing).

    Returns ``202 Accepted`` with the order/payment identifiers on success.
    """

    permission_classes = [AllowAny]
    throttle_classes = [TenantUserScopedThrottle]
    throttle_scope = "checkout"

    @extend_schema(
        operation_id="cart_checkout",
        summary="Checkout the caller's active cart",
        description=(
            "Initiates the checkout flow for the caller's active cart under a "
            "**distributed Redis lock** and a **Postgres row lock**, guaranteeing "
            "exactly-one order creation per cart.\n\n"
            "### Pre-conditions\n\n"
            "Before calling this endpoint:\n"
            "1. Call `POST /cart/add-address/` to select a shipping address.\n"
            "2. Call `POST /cart/add-payment-method/` to select a payment method.\n\n"
            "Missing either → `422 cart/checkout-incomplete`.\n\n"
            "### Idempotency\n\n"
            "Supply a client-generated `Idempotency-Key` UUID. Repeating the "
            "request with the **same key** returns the stored `202` without "
            "re-executing the checkout. A different key on the same cart creates "
            "a second attempt (use only if the first truly failed).\n\n"
            "### Cart resolution\n\n"
            "No `cart_id` is required — the active cart is resolved from "
            "`X-Tenant-Domain` + `X-User-Id`. After a successful checkout the "
            "cart is marked `CHECKED_OUT` and the constraint "
            "`uq_cart_one_active_per_tenant_user` allows a fresh ACTIVE cart to "
            "be created for the next purchase."
        ),
        tags=["Cart"],
        parameters=[
            TENANT_DOMAIN_HEADER,
            USER_ID_HEADER,
            IDEMPOTENCY_KEY_HEADER,
            X_REQUEST_ID_HEADER,
        ],
        request=CartCheckoutSerializer,
        responses={
            202: OpenApiResponse(
                response=CheckoutResponseSerializer,
                description=(
                    "Checkout accepted. Order and payment records created. "
                    "Payment authorisation is pending on the async Celery worker. "
                    "Also returned on idempotent replay."
                ),
            ),
            **_CART_COMMON_ERRORS,
            400: problem_response(
                400,
                "validation/idempotency-key-required",
                "Idempotency-Key header is required or invalid",
                "Supply a UUID in the Idempotency-Key header.",
            ),
            404: problem_response(
                404,
                "cart/not-found",
                "Cart not found",
                "cart a1b2c3d4-… not found",
                description=(
                    "Returned when no active cart exists for the resolved "
                    "`X-Tenant-Domain` + `X-User-Id` combination."
                ),
            ),
            409: problem_response(
                409,
                "cart/locked",
                "Conflict — cart locked or idempotency issue",
                "A concurrent checkout is already in progress for this cart.",
                description=(
                    "Returned in three scenarios:\n\n"
                    "| `type` | Cause | Client action |\n"
                    "|--------|-------|---------------|\n"
                    "| `cart/locked` | Redis lock already held | Retry after ~1 s back-off |\n"
                    "| `cart/already-checked-out` | Cart already checked out | Create a new cart |\n"
                    "| `idempotency/conflict` | Same key, different request | Use a new key |\n"
                    "| `idempotency/in-progress` | Duplicate in-flight request | Retry after short back-off |\n"
                ),
            ),
            422: problem_response(
                422,
                "cart/checkout-incomplete",
                "Address or payment method not selected",
                "Call POST /cart/add-address before checking out.",
                description=(
                    "Returned when the cart is missing `selected_address` or "
                    "`selected_payment_method`, or when a domain rule is violated "
                    "(empty cart, out-of-stock product, expired coupon)."
                ),
            ),
            429: _RATE_LIMIT_RESPONSE,
        },
        examples=[
            *checkout_request_examples(),
            *checkout_response_examples(),
        ],
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        # Validate (empty) body — present for OpenAPI schema consistency.
        serializer = CartCheckoutSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        # Parse and validate Idempotency-Key header.
        raw_key = request.headers.get("Idempotency-Key", "").strip()
        if not raw_key:
            return problem(
                "validation/idempotency-key-required",
                "Idempotency-Key header is required",
                400,
                "Supply a UUID in the Idempotency-Key header for this endpoint.",
            )
        try:
            idempotency_key = uuid.UUID(raw_key)
        except ValueError:
            return problem(
                "validation/idempotency-key-invalid",
                "Idempotency-Key must be a valid UUID",
                400,
                f"'{raw_key}' is not a valid UUID.",
            )

        # ------------------------------------------------------------------
        # Preliminary idempotency fast-path (before cart resolution).
        #
        # The active cart changes after a successful checkout (the checked-out
        # cart is replaced by a new ACTIVE one).  If we resolved the cart
        # first, a replay would see the new empty cart and return 422 before
        # the durable record is found.  We therefore do a quick Postgres
        # lookup for any SUCCEEDED record under this key first; if found we
        # return the stored response immediately without touching the cart.
        # ------------------------------------------------------------------
        from apps.core.models import IdempotencyRecord
        from apps.tenant.context import get_current_tenant

        tenant_obj = get_current_tenant()
        if tenant_obj is not None:
            _prior = IdempotencyRecord.objects.filter(
                tenant_id=tenant_obj.id,
                key=idempotency_key,
                status=IdempotencyRecord.Status.SUCCEEDED,
            ).first()
            if _prior is not None:
                body = _prior.response_body or {}
                out = CheckoutResponseSerializer(
                    {
                        "order_id": body.get("order_id"),
                        "payment_id": body.get("payment_id"),
                        "payment_status": body.get("payment_status", "pending"),
                        "total": body.get("total", "0.00"),
                        "currency": body.get("currency", ""),
                        "company_name": body.get("company_name", ""),
                        "tax_number": body.get("tax_number", ""),
                        "purchase_order_reference": body.get(
                            "purchase_order_reference", ""
                        ),
                    }
                )
                return Response(out.data, status=_prior.response_status or 202)

        # Resolve the active cart.
        cart = get_or_create_active_cart(user_id)

        # Enforce that address + payment method have been selected.
        if cart.selected_address_id is None:
            return problem(
                "cart/checkout-incomplete",
                "No shipping address selected",
                422,
                "Call POST /cart/add-address before checking out.",
            )
        if cart.selected_payment_method_id is None:
            return problem(
                "cart/checkout-incomplete",
                "No payment method selected",
                422,
                "Call POST /cart/add-payment-method before checking out.",
            )

        # Compute the request hash for idempotency conflict detection.
        request_hash = compute_request_hash(
            {
                "cart_id": str(cart.id),
                "address_id": str(cart.selected_address_id),
                "payment_method_id": str(cart.selected_payment_method_id),
            }
        )

        service = CheckoutService()
        try:
            result = service.checkout(
                cart_id=cart.id,
                payment_method_id=cart.selected_payment_method_id,
                address_id=cart.selected_address_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except (
            OrderDomainError,
            CouponDomainError,
            LockNotAcquired,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as exc:
            return map_exception(exc)

        out = CheckoutResponseSerializer(
            {
                "order_id": result.order_id,
                "payment_id": result.payment_id,
                "payment_status": result.payment_status,
                "total": result.total,
                "currency": result.currency,
                "company_name": result.company_name,
                "tax_number": result.tax_number,
                "purchase_order_reference": result.purchase_order_reference,
            }
        )
        return Response(out.data, status=result.http_status)


# ---------------------------------------------------------------------------
# Canonical RESTful views (PROJECT_SPEC §5.4 — preferred API surface)
#
# These views implement the same business logic as the legacy action-style
# views above, but expose resource-oriented URLs:
#
#   POST   /api/v1/cart/items/
#   DELETE /api/v1/cart/items/{product_id}/
#   POST   /api/v1/cart/coupons/
#   DELETE /api/v1/cart/coupons/{coupon_id}/
#   PUT    /api/v1/cart/address/
#   PUT    /api/v1/cart/payment-method/
#   PUT    /api/v1/cart/business-details/
#
# Each view calls the same service functions as its legacy counterpart so
# there is zero duplication of business logic.
# ---------------------------------------------------------------------------


class CartItemsView(APIView):
    """``POST /api/v1/cart/items/``

    Add (or top-up) a product line on the caller's active cart.

    Canonical RESTful alias for ``POST /api/v1/cart/add-product/``.
    Request body: ``{"product_id": "<uuid>", "quantity": <int>}``
    """

    permission_classes = [AllowAny]
    throttle_classes = [TenantUserScopedThrottle]
    throttle_scope = "add_product"

    @extend_schema(
        operation_id="cart_items_add",
        summary="Add a product to the active cart",
        description=(
            "Adds `quantity` units of `product_id` to the caller's active cart, "
            "creating the cart if it does not yet exist.\n\n"
            "If the product is already in the cart the existing quantity is "
            "**incremented** — no duplicate line is created.\n\n"
            "Rate limit: **60 requests / minute** per tenant + user."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=AddProductSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart after product was added.",
            ),
            **_CART_COMMON_ERRORS,
            404: problem_response(
                404,
                "product/not-found",
                "Product not found",
                "product cccccccc-… not found in this tenant's catalog",
                description=(
                    "Returned when `product_id` does not exist within the current tenant."
                ),
            ),
            429: _RATE_LIMIT_RESPONSE,
        },
        examples=add_product_examples(),
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = AddProductSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        data = serializer.validated_data
        product_id: UUID = data["product_id"]
        quantity: int = data["quantity"]

        try:
            product = Product.objects.get(pk=product_id)
        except Product.DoesNotExist:
            return problem(
                "product/not-found",
                "Product not found",
                404,
                f"product {product_id} not found in this tenant's catalog",
            )

        cart = get_or_create_active_cart(user_id)

        try:
            cart = add_product_to_cart(cart, product, quantity)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class CartItemDetailView(APIView):
    """``DELETE /api/v1/cart/items/{product_id}/``

    Remove the line for a product from the caller's active cart.  Idempotent.

    Canonical RESTful alias for ``POST /api/v1/cart/remove-product/``.
    The ``product_id`` is taken from the URL path, not the request body.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_items_remove",
        summary="Remove a product from the active cart",
        description=(
            "Removes the line for `product_id` from the caller's active cart.\n\n"
            "**Idempotent** — if the product is not in the cart the call "
            "succeeds silently and returns the unchanged cart."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart after product was removed.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=remove_product_examples(),
    )
    def delete(self, request: Request, product_id: UUID) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        cart = get_or_create_active_cart(user_id)

        try:
            cart = remove_product_from_cart(cart, product_id)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class CartCouponsView(APIView):
    """``POST /api/v1/cart/coupons/``

    Apply a coupon code to the caller's active cart.

    Canonical RESTful alias for ``POST /api/v1/cart/add-coupon/``.
    Request body: ``{"code": "<coupon-code>"}``
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_coupons_apply",
        summary="Apply a coupon to the active cart",
        description=(
            "Validates and applies a coupon code to the caller's active cart. "
            "The discount is computed immediately and stored as a snapshot on "
            "`CartCoupon.discount_amount`.\n\n"
            "Returns `422` when the coupon is expired, usage-limit-reached, or "
            "already applied to this cart."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=ApplyCouponSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart with coupon applied.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=apply_coupon_examples(),
    )
    def post(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = ApplyCouponSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        code: str = serializer.validated_data["code"]
        cart = get_or_create_active_cart(user_id)

        try:
            cart = _COUPON_SERVICE.apply_coupon_to_cart(cart, code)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class CartCouponDetailView(APIView):
    """``DELETE /api/v1/cart/coupons/{coupon_id}/``

    Remove a coupon from the caller's active cart by coupon UUID.  Idempotent.

    Canonical RESTful alias for ``POST /api/v1/cart/remove-coupon/``.
    The ``coupon_id`` is taken from the URL path, not the request body.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_coupons_remove",
        summary="Remove a coupon from the active cart",
        description=(
            "Removes the applied coupon identified by `coupon_id` from the "
            "caller's active cart and recalculates the totals.\n\n"
            "**Idempotent** — removing a coupon that is not applied is a no-op."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart after coupon was removed.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=remove_coupon_examples(),
    )
    def delete(self, request: Request, coupon_id: UUID) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        cart = get_or_create_active_cart(user_id)

        try:
            cart = _COUPON_SERVICE.remove_coupon_from_cart(cart, coupon_id)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class CartAddressView(APIView):
    """``PUT /api/v1/cart/address/``

    Create a new shipping address for the caller and set it as the cart's
    selected address.

    Canonical RESTful alias for ``POST /api/v1/cart/add-address/``.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_address_set",
        summary="Set a shipping address on the active cart",
        description=(
            "Creates a new `Address` record for the customer and immediately "
            "sets it as the `selected_address` on their active cart.\n\n"
            "This address is required before calling `POST /cart/checkout/`."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=AddAddressSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart with the new address selected.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=add_address_examples(),
    )
    def put(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = AddAddressSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        data = serializer.validated_data
        cart = get_or_create_active_cart(user_id)

        try:
            address = add_address(
                user_id=user_id,
                country=data["country"],
                city=data["city"],
                details=data["details"],
                label=data.get("label", ""),
                is_default=data.get("is_default", False),
            )
            cart = set_cart_address(cart, address)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class CartPaymentMethodView(APIView):
    """``PUT /api/v1/cart/payment-method/``

    Create a new payment method and set it as the cart's selected payment
    method.

    Canonical RESTful alias for ``POST /api/v1/cart/add-payment-method/``.
    Request body: ``{"gateway_slug": "dummy_success"}``
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_payment_method_set",
        summary="Set a payment method on the active cart",
        description=(
            "Creates a new `PaymentMethod` record linked to the given gateway "
            "and immediately sets it as the `selected_payment_method` on the "
            "caller's active cart.\n\n"
            "This payment method is required before calling `POST /cart/checkout/`. "
            "Returns `422` if `gateway_slug` is not a registered gateway."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=AddPaymentMethodSerializer,
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart with the new payment method selected.",
            ),
            **_CART_COMMON_ERRORS,
        },
        examples=add_payment_method_examples(),
    )
    def put(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = AddPaymentMethodSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        gateway_slug: str = serializer.validated_data["gateway_slug"]
        cart = get_or_create_active_cart(user_id)

        try:
            payment_method = add_payment_method(gateway_slug=gateway_slug)
            cart = set_cart_payment_method(cart, payment_method)
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)


class CartBusinessDetailsView(APIView):
    """``PUT /api/v1/cart/business-details/``

    Set B2B buyer-metadata on the caller's active cart.

    Canonical RESTful alias for ``POST /api/v1/cart/set-business-details/``.
    All fields are optional; at least one must be non-empty.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="cart_business_details_set",
        summary="Set B2B business details on the active cart",
        description=(
            "Stores company name, tax/VAT number, and/or purchase-order "
            "reference on the caller's active cart.\n\n"
            "All three fields are optional individually, but at least one "
            "must be non-empty. They are visible in `GET /api/v1/cart/` "
            "immediately and are snapshotted onto the Order at checkout time.\n\n"
            "Calling this endpoint is idempotent — repeated calls overwrite "
            "the previous values."
        ),
        tags=["Cart"],
        parameters=[TENANT_DOMAIN_HEADER, USER_ID_HEADER, X_REQUEST_ID_HEADER],
        request=SetBusinessDetailsSerializer,
        examples=b2b_request_examples(),
        responses={
            200: OpenApiResponse(
                response=CartReadSerializer,
                description="Updated cart with B2B details applied.",
            ),
            **_CART_COMMON_ERRORS,
        },
    )
    def put(self, request: Request) -> Response:
        user_id, err = _user_id_required(request)
        if err:
            return err

        serializer = SetBusinessDetailsSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_problem(serializer.errors)

        data = serializer.validated_data
        cart = get_or_create_active_cart(user_id)

        try:
            cart = set_business_details(
                cart,
                company_name=data.get("company_name", ""),
                tax_number=data.get("tax_number", ""),
                purchase_order_reference=data.get("purchase_order_reference", ""),
            )
        except Exception as exc:
            return map_exception(exc)

        return _cart_response(cart)
