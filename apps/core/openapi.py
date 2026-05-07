"""OpenAPI building blocks shared across all apps.

Centralises the reusable drf-spectacular primitives so that every view that
needs to declare the ``X-Tenant-Domain`` header or a RFC 7807 error response
does not have to repeat the same boilerplate.

Public API
----------
``TENANT_DOMAIN_HEADER``
    ``OpenApiParameter`` for the required ``X-Tenant-Domain`` header.

``USER_ID_HEADER``
    ``OpenApiParameter`` for the required ``X-User-Id`` header.

``IDEMPOTENCY_KEY_HEADER``
    ``OpenApiParameter`` for the required ``Idempotency-Key`` header.

``X_REQUEST_ID_HEADER``
    ``OpenApiParameter`` for the optional ``X-Request-Id`` tracing header.

``ProblemDetailSerializer``
    Inline serializer describing a RFC 7807 ``application/problem+json``
    response body.  Use with ``inline_serializer`` or directly in
    ``@extend_schema(responses={4xx: ProblemDetailSerializer()})``.

``problem_response(status, type_slug, title, detail_example)``
    Factory that returns the ``OpenApiResponse`` dict expected by
    ``@extend_schema(responses=...)``.

``cart_read_response_examples()``
    Response examples for ``GET /api/v1/cart/``.

``add_product_examples()``
    Request + response examples for ``POST /api/v1/cart/add-product/``.

``remove_product_examples()``
    Request + response examples for ``POST /api/v1/cart/remove-product/``.

``apply_coupon_examples()``
    Request + response examples for ``POST /api/v1/cart/add-coupon/``.

``remove_coupon_examples()``
    Request + response examples for ``POST /api/v1/cart/remove-coupon/``.

``add_address_examples()``
    Request + response examples for ``POST /api/v1/cart/add-address/``.

``add_payment_method_examples()``
    Request + response examples for ``POST /api/v1/cart/add-payment-method/``.

``b2b_request_examples()``
    Request body examples for ``POST /api/v1/cart/set-business-details/``.

``checkout_request_examples()``
    Request body examples for the checkout endpoints.

``checkout_response_examples()``
    Response examples for the checkout endpoints: success, idempotent replay,
    out-of-stock, coupon expired, and invalid coupon code.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    inline_serializer,
)
from rest_framework import serializers

# ---------------------------------------------------------------------------
# Shared header parameters
# ---------------------------------------------------------------------------

TENANT_DOMAIN_HEADER = OpenApiParameter(
    name="X-Tenant-Domain",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    required=True,
    description=(
        "The tenant's unique domain as registered in the platform "
        "(e.g. `acme.mysaas.com`). Every non-exempt endpoint requires this "
        "header. Missing → `400 tenant/missing-header`. "
        "Unknown domain → `404 tenant/not-found`. "
        "Inactive tenant → `403 tenant/disabled`."
    ),
    examples=[
        OpenApiExample(
            name="Acme store",
            value="acme.mysaas.com",
            summary="A live tenant domain",
        ),
        OpenApiExample(
            name="Dev sandbox",
            value="dev.localhost",
            summary="Local development",
        ),
    ],
)

USER_ID_HEADER = OpenApiParameter(
    name="X-User-Id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.HEADER,
    required=True,
    description=(
        "UUID of the authenticated customer.\n\n"
        "This is the **interim** user-identity contract until bearer-token "
        "authentication is wired at the API gateway layer (PROJECT_SPEC §4.3). "
        "The gateway will inject this header after validating the bearer token; "
        "until then clients supply it directly.\n\n"
        "- Missing or non-UUID value → `400 validation/user-id-required`.\n"
        "- For the resource-oriented `POST /carts/{cart_id}/checkout/` endpoint, "
        "supplying this header causes the server to verify that the cart's "
        "`user_id` matches; a mismatch → `403 cart/forbidden`."
    ),
    examples=[
        OpenApiExample(
            name="Customer UUID",
            value="d290f1ee-6c54-4b01-90e6-d701748f0851",
            summary="Typical customer identifier",
        ),
    ],
)

X_REQUEST_ID_HEADER = OpenApiParameter(
    name="X-Request-Id",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    required=False,
    description=(
        "Optional client-supplied correlation id (max 128 chars). "
        "If absent, the server generates a UUID4 and echoes it back in the "
        "`X-Request-Id` response header. "
        "Appears in every structured log record for end-to-end tracing across "
        "Django, Celery workers, and the invoice service."
    ),
    examples=[
        OpenApiExample(
            name="Trace id",
            value="my-client-trace-abc123",
            summary="Client-supplied correlation id",
        ),
    ],
)

IDEMPOTENCY_KEY_HEADER = OpenApiParameter(
    name="Idempotency-Key",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.HEADER,
    required=True,
    description=(
        "A client-generated UUID (v1–v5) that uniquely identifies this request "
        "attempt. The server stores the first response for 24 hours and returns "
        "it verbatim on replay without re-executing side-effects.\n\n"
        "**Replay rules:**\n"
        "- Same key **+** same body → `202` (or whatever the original status was).\n"
        "- Same key **+** different body → `409 idempotency/conflict`.\n"
        "- Same key while the original request is still processing → "
        "`409 idempotency/in-progress` (retry after a short back-off).\n\n"
        "Generate a fresh UUID per logical request attempt, not per HTTP retry."
    ),
    examples=[
        OpenApiExample(
            name="Fresh checkout attempt",
            value="550e8400-e29b-41d4-a716-446655440000",
            summary="New UUID for first attempt",
        ),
    ],
)

# ---------------------------------------------------------------------------
# RFC 7807 problem+json serializer (for response schema documentation)
# ---------------------------------------------------------------------------


class ProblemDetailSerializer(serializers.Serializer):
    """RFC 7807 Problem Details object (``application/problem+json``).

    Returned by all error responses in cart_system.  The ``type`` field is a
    URI uniquely identifying the error class.  ``title`` is a short, human-
    readable summary.  ``status`` mirrors the HTTP status code.  ``detail``
    carries the instance-specific diagnostic message.  Additional members
    (e.g. ``product_id``) may be present for machine-readable context.
    """

    type = serializers.CharField(
        help_text=(
            "A URI that identifies the problem type. "
            "Example: `https://cart-system.local/problems/cart/empty`"
        )
    )
    title = serializers.CharField(
        help_text="Short, human-readable summary of the problem type."
    )
    status = serializers.IntegerField(
        help_text="HTTP status code (mirrors the response status line)."
    )
    detail = serializers.CharField(
        help_text="Human-readable explanation specific to this occurrence."
    )


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def problem_response(
    status: int,
    type_slug: str,
    title: str,
    detail_example: str,
    description: str = "",
    **extra_fields: str,
) -> OpenApiResponse:
    """Build an ``OpenApiResponse`` for a RFC 7807 error code.

    Args:
        status:           HTTP status code (400, 404, 409, 422, …).
        type_slug:        Last path segment of the problem type URI.
                          Full URI = ``https://cart-system.local/problems/{type_slug}``.
        title:            Short summary displayed in the OpenAPI UI.
        detail_example:   Example ``detail`` string for the generated example.
        description:      Markdown description shown in the response list.
        **extra_fields:   Additional k=v pairs included in the example body.

    Returns:
        ``OpenApiResponse`` suitable for use in
        ``@extend_schema(responses={status: problem_response(...)})``.
    """
    base_url = "https://cart-system.local/problems"
    example_body: dict = {
        "type": f"{base_url}/{type_slug}",
        "title": title,
        "status": status,
        "detail": detail_example,
    }
    example_body.update(extra_fields)

    return OpenApiResponse(
        response=ProblemDetailSerializer,
        description=description or title,
        examples=[
            OpenApiExample(
                name=title,
                value=example_body,
                response_only=True,
                media_type="application/problem+json",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Checkout-specific examples (request + response)
# ---------------------------------------------------------------------------


def b2b_request_examples() -> list[OpenApiExample]:
    """Two request body examples for the set-business-details endpoint."""
    return [
        OpenApiExample(
            name="Full B2B details",
            summary="All three B2B fields supplied",
            description=(
                "A corporate buyer supplies their company name, VAT number, and "
                "internal PO reference so all three appear on the invoice."
            ),
            value={
                "company_name": "Acme Corp Ltd",
                "tax_number": "GB123456789",
                "purchase_order_reference": "PO-2026-00042",
            },
            request_only=True,
        ),
        OpenApiExample(
            name="PO reference only",
            summary="Only the purchase-order reference is required",
            description=(
                "Minimal B2B call: the buyer provides only the PO reference for "
                "internal reconciliation. company_name and tax_number default to ''."
            ),
            value={
                "purchase_order_reference": "PO-2026-00099",
            },
            request_only=True,
        ),
    ]


def checkout_request_examples() -> list[OpenApiExample]:
    """Four request body examples for the checkout endpoint."""
    return [
        OpenApiExample(
            name="Standard checkout",
            summary="First-time checkout with a Stripe card",
            description=(
                "The most common case: a cart with items, a stored card, and a "
                "shipping address.  Use a fresh UUID for `Idempotency-Key`."
            ),
            value={
                "payment_method_id": "11111111-0000-0000-0000-000000000001",
                "address_id": "22222222-0000-0000-0000-000000000002",
            },
            request_only=True,
        ),
        OpenApiExample(
            name="Idempotent replay",
            summary="Retrying a timed-out request",
            description=(
                "Use the **same** `Idempotency-Key` and the **same** body as the "
                "original timed-out call. The server returns the stored `202` without "
                "creating a second order."
            ),
            value={
                "payment_method_id": "11111111-0000-0000-0000-000000000001",
                "address_id": "22222222-0000-0000-0000-000000000002",
            },
            request_only=True,
        ),
    ]


def checkout_response_examples() -> list[OpenApiExample]:
    """Five response examples for the checkout endpoint (202 and errors)."""
    return [
        OpenApiExample(
            name="202 — Checkout accepted",
            summary="Payment pending async authorisation",
            description=(
                "Checkout committed to the database. The Celery `payments` worker "
                "is authorising the charge asynchronously. Poll the payment endpoint "
                "or wait for the webhook to confirm `AUTHORIZED`."
            ),
            value={
                "order_id": "aaaaaaaa-0000-0000-0000-000000000001",
                "payment_id": "bbbbbbbb-0000-0000-0000-000000000002",
                "payment_status": "pending",
                "total": "149.99",
                "currency": "USD",
            },
            response_only=True,
            status_codes=["202"],
        ),
        OpenApiExample(
            name="202 — Idempotent replay",
            summary="Same response returned for a duplicate request",
            description=(
                "The server found a matching `IdempotencyRecord` (same key + same "
                "body) and returned the stored `202` without touching the database or "
                "dispatching a second payment task."
            ),
            value={
                "order_id": "aaaaaaaa-0000-0000-0000-000000000001",
                "payment_id": "bbbbbbbb-0000-0000-0000-000000000002",
                "payment_status": "pending",
                "total": "149.99",
                "currency": "USD",
            },
            response_only=True,
            status_codes=["202"],
        ),
        OpenApiExample(
            name="422 — Product out of stock",
            summary="Stock exhausted between cart build and checkout",
            description=(
                "A concurrent checkout (or admin stock adjustment) reduced a product's "
                "stock to zero between the time the item was added to the cart and the "
                "checkout attempt. The client should refresh the cart and remove or "
                "reduce the affected item."
            ),
            value={
                "type": "https://cart-system.local/problems/product/out-of-stock",
                "title": "A product is out of stock",
                "status": 422,
                "detail": "product cccccccc-0000-0000-0000-000000000003 is out of stock",
                "product_id": "cccccccc-0000-0000-0000-000000000003",
            },
            response_only=True,
            status_codes=["422"],
            media_type="application/problem+json",
        ),
        OpenApiExample(
            name="422 — Coupon expired",
            summary="Coupon became invalid after being applied to cart",
            description=(
                "The coupon was valid when it was applied to the cart but expired "
                "(or was deactivated) before checkout completed. The `revalidate_cart_coupons` "
                "hook inside the checkout transaction detected the drift. The client "
                "should remove the coupon and retry."
            ),
            value={
                "type": "https://cart-system.local/problems/coupon/expired",
                "title": "Coupon validation failed",
                "status": 422,
                "detail": "coupon SUMMER25 has expired",
            },
            response_only=True,
            status_codes=["422"],
            media_type="application/problem+json",
        ),
        OpenApiExample(
            name="422 — Invalid coupon code",
            summary="Coupon code not found in this tenant",
            description=(
                "The coupon code supplied does not exist within the current tenant's "
                "catalog, or has already been fully redeemed. The client should prompt "
                "the customer to re-enter a valid code."
            ),
            value={
                "type": "https://cart-system.local/problems/coupon/not-found",
                "title": "Coupon not found",
                "status": 422,
                "detail": "coupon BOGUS99 not found in tenant acme.mysaas.com",
            },
            response_only=True,
            status_codes=["422"],
            media_type="application/problem+json",
        ),
    ]


# ---------------------------------------------------------------------------
# Cart action endpoint examples
# ---------------------------------------------------------------------------

_CART_RESPONSE_EXAMPLE = {
    "id": "dddddddd-0000-0000-0000-000000000004",
    "user_id": "d290f1ee-6c54-4b01-90e6-d701748f0851",
    "status": "ACTIVE",
    "currency": "USD",
    "total_price": "149.99",
    "discount_amount": "15.00",
    "total_after_discount": "134.99",
    "version": 3,
    "company_name": "",
    "tax_number": "",
    "purchase_order_reference": "",
    "items": [
        {
            "id": "eeeeeeee-0000-0000-0000-000000000005",
            "product_id": "cccccccc-0000-0000-0000-000000000003",
            "quantity": 2,
            "price_snapshot": "59.99",
            "currency": "USD",
        },
        {
            "id": "ffffffff-0000-0000-0000-000000000006",
            "product_id": "11111111-1111-1111-1111-111111111111",
            "quantity": 1,
            "price_snapshot": "30.01",
            "currency": "USD",
        },
    ],
    "applied_coupons": [
        {
            "id": "aaaaaaaa-1111-0000-0000-000000000007",
            "coupon_id": "bbbbbbbb-2222-0000-0000-000000000008",
            "code": "SUMMER25",
            "discount_amount": "15.00",
            "currency": "USD",
        }
    ],
    "selected_address": {
        "id": "22222222-0000-0000-0000-000000000002",
        "country": "US",
        "city": "Springfield",
        "details": "742 Evergreen Terrace",
        "label": "home",
    },
    "selected_payment_method": {
        "id": "11111111-0000-0000-0000-000000000001",
        "gateway_slug": "dummy_success",
    },
}


def cart_read_response_examples() -> list[OpenApiExample]:
    """Response examples for ``GET /api/v1/cart/``."""
    return [
        OpenApiExample(
            name="Active cart with items",
            summary="Cart with two products, one coupon, address, and payment method",
            description=(
                "A fully-populated cart. `total_after_discount` is the payable amount "
                "after all applied coupons have been deducted. `version` is the "
                "optimistic-concurrency token — it increments on every mutation."
            ),
            value=_CART_RESPONSE_EXAMPLE,
            response_only=True,
            status_codes=["200"],
        ),
        OpenApiExample(
            name="Empty cart (just created)",
            summary="Freshly auto-created cart — no items yet",
            description=(
                "Returned on first call when no ACTIVE cart exists for the "
                "`X-Tenant-Domain` + `X-User-Id` combination. The cart is transparently "
                "created and returned in a single round-trip."
            ),
            value={
                "id": "99999999-0000-0000-0000-000000000099",
                "user_id": "d290f1ee-6c54-4b01-90e6-d701748f0851",
                "status": "ACTIVE",
                "currency": "USD",
                "total_price": "0.00",
                "discount_amount": "0.00",
                "total_after_discount": "0.00",
                "version": 1,
                "company_name": "",
                "tax_number": "",
                "purchase_order_reference": "",
                "items": [],
                "applied_coupons": [],
                "selected_address": None,
                "selected_payment_method": None,
            },
            response_only=True,
            status_codes=["200"],
        ),
    ]


def add_product_examples() -> list[OpenApiExample]:
    """Request and response examples for ``POST /api/v1/cart/add-product/``."""
    return [
        OpenApiExample(
            name="Add 2 units of a product",
            summary="Add a new product line to the cart",
            description=(
                "If the product is already in the cart the quantity is incremented; "
                "no duplicate line is created."
            ),
            value={"product_id": "cccccccc-0000-0000-0000-000000000003", "quantity": 2},
            request_only=True,
        ),
        OpenApiExample(
            name="Cart after adding product",
            summary="Updated cart returned immediately",
            value=_CART_RESPONSE_EXAMPLE,
            response_only=True,
            status_codes=["200"],
        ),
    ]


def remove_product_examples() -> list[OpenApiExample]:
    """Request and response examples for ``POST /api/v1/cart/remove-product/``."""
    return [
        OpenApiExample(
            name="Remove a product",
            summary="Remove a product line from the cart",
            description=(
                "Idempotent — removing a product that is not in the cart is a no-op "
                "and the unmodified cart is returned."
            ),
            value={"product_id": "cccccccc-0000-0000-0000-000000000003"},
            request_only=True,
        ),
        OpenApiExample(
            name="Cart after removing product",
            summary="Updated cart with the product line removed",
            value={
                **_CART_RESPONSE_EXAMPLE,
                "items": [],
                "total_price": "30.01",
                "total_after_discount": "15.01",
                "version": 4,
            },
            response_only=True,
            status_codes=["200"],
        ),
    ]


def apply_coupon_examples() -> list[OpenApiExample]:
    """Request and response examples for ``POST /api/v1/cart/add-coupon/``."""
    return [
        OpenApiExample(
            name="Apply SUMMER25 coupon",
            summary="Apply a percentage-discount coupon to the cart",
            description=(
                "The discount is computed immediately and stored as a snapshot "
                "on `CartCoupon.discount_amount`. The cart totals are recalculated "
                "atomically."
            ),
            value={"code": "SUMMER25"},
            request_only=True,
        ),
        OpenApiExample(
            name="Cart after coupon applied",
            summary="Cart with the coupon deducted from the total",
            value=_CART_RESPONSE_EXAMPLE,
            response_only=True,
            status_codes=["200"],
        ),
    ]


def remove_coupon_examples() -> list[OpenApiExample]:
    """Request and response examples for ``POST /api/v1/cart/remove-coupon/``."""
    return [
        OpenApiExample(
            name="Remove a coupon",
            summary="Remove an applied coupon by its UUID",
            description=(
                "Idempotent — removing a coupon that is not applied is a no-op. "
                "The cart totals are recalculated after removal."
            ),
            value={"coupon_id": "bbbbbbbb-2222-0000-0000-000000000008"},
            request_only=True,
        ),
        OpenApiExample(
            name="Cart after coupon removed",
            summary="Cart with the coupon removed and totals recalculated",
            value={
                **_CART_RESPONSE_EXAMPLE,
                "applied_coupons": [],
                "discount_amount": "0.00",
                "total_after_discount": "149.99",
                "version": 4,
            },
            response_only=True,
            status_codes=["200"],
        ),
    ]


def add_address_examples() -> list[OpenApiExample]:
    """Request and response examples for ``POST /api/v1/cart/add-address/``."""
    return [
        OpenApiExample(
            name="US shipping address",
            summary="Create and select a US shipping address",
            description=(
                "Creates a new `Address` record and immediately sets it as the "
                "`selected_address` on the cart. Required before calling checkout."
            ),
            value={
                "country": "US",
                "city": "Springfield",
                "details": "742 Evergreen Terrace, IL 62701",
                "label": "home",
                "is_default": True,
            },
            request_only=True,
        ),
        OpenApiExample(
            name="Cart after address added",
            summary="Cart with the new address selected",
            value=_CART_RESPONSE_EXAMPLE,
            response_only=True,
            status_codes=["200"],
        ),
    ]


def add_payment_method_examples() -> list[OpenApiExample]:
    """Request and response examples for ``POST /api/v1/cart/add-payment-method/``."""
    return [
        OpenApiExample(
            name="Add dummy gateway",
            summary="Register a payment method using the test gateway",
            description=(
                "Creates a `PaymentMethod` linked to the given gateway slug and "
                "sets it as `selected_payment_method` on the cart. Required before "
                "calling checkout. Use `dummy_success` in non-production environments."
            ),
            value={"gateway_slug": "dummy_success"},
            request_only=True,
        ),
        OpenApiExample(
            name="Cart after payment method added",
            summary="Cart with the new payment method selected",
            value=_CART_RESPONSE_EXAMPLE,
            response_only=True,
            status_codes=["200"],
        ),
    ]
