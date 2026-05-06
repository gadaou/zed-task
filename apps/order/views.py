"""Order views — checkout endpoint.

Implements ``POST /v1/carts/{cart_id}/checkout`` per PROJECT_SPEC §2 op 7.

Architecture:
    - Views are thin: parse → validate → dispatch → serialise.
    - No business logic here; everything lives in ``CheckoutService``.
    - Exception mapping converts typed domain errors to RFC 7807 problem+json
      (PROJECT_SPEC §6.4).  The global problem+json handler lands with
      ``apps.core`` in a later iteration; until then each view carries a small
      inline mapping.

Response codes (per PROJECT_SPEC §5.3 / §5.4):
    - 202 Accepted  — checkout committed; payment authorisation pending.
    - 200 OK        — inline gateway, payment authorised synchronously.
                      (Not used in this iteration; the mock gateway always
                      returns 202.)
    - 400 Bad Request   — invalid body or missing Idempotency-Key header.
    - 404 Not Found     — cart, address, or payment method not found.
    - 409 Conflict      — idempotency conflict/in-progress, or cart locked/stale.
    - 422 Unprocessable — business-rule failure (out of stock, coupon invalid,
                          empty cart, already checked out).
"""

from __future__ import annotations

import logging
import uuid

from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse
from drf_spectacular.types import OpenApiTypes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import (
    IdempotencyConflict,
    IdempotencyInProgress,
    LockNotAcquired,
)
from apps.core.openapi import (
    IDEMPOTENCY_KEY_HEADER,
    TENANT_DOMAIN_HEADER,
    checkout_request_examples,
    checkout_response_examples,
    problem_response,
)
from apps.coupon.exceptions import CouponDomainError
from apps.order.exceptions import (
    AddressNotFound,
    CartAlreadyCheckedOut,
    CartEmpty,
    CartNotFound,
    CartStaleVersion,
    OrderDomainError,
    PaymentMethodInvalid,
    ProductOutOfStock,
)
from apps.order.serializers import CheckoutRequestSerializer, CheckoutResponseSerializer
from apps.order.services import CheckoutService
from apps.core.idempotency import compute_request_hash

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RFC 7807 problem+json helpers
# ---------------------------------------------------------------------------

def _problem(type_: str, title: str, status: int, detail: str, **extra) -> Response:
    """Build a minimal RFC 7807 problem+json response dict."""
    body = {
        "type": f"https://cart-system.local/problems/{type_}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    body.update(extra)
    return Response(body, status=status, content_type="application/problem+json")


# ---------------------------------------------------------------------------
# Exception → HTTP mapping
# ---------------------------------------------------------------------------

_EXCEPTION_MAP: dict[type, tuple[int, str]] = {
    # 404 Not Found
    CartNotFound: (404, "Cart not found"),
    AddressNotFound: (404, "Address not found"),
    PaymentMethodInvalid: (404, "Payment method not found"),
    # 409 Conflict
    CartStaleVersion: (409, "Cart was modified concurrently — retry"),
    CartAlreadyCheckedOut: (409, "Cart is already checked out"),
    LockNotAcquired: (409, "A concurrent checkout is already in progress"),
    IdempotencyConflict: (409, "Idempotency-Key reused with a different request"),
    IdempotencyInProgress: (409, "Request with this Idempotency-Key is in progress"),
    # 422 Unprocessable (business-rule failures)
    CartEmpty: (422, "Cart is empty"),
    ProductOutOfStock: (422, "A product is out of stock"),
}


def _map_exception(exc: Exception) -> Response:
    """Convert a domain exception to an RFC 7807 problem+json response."""
    exc_type = type(exc)

    if exc_type in _EXCEPTION_MAP:
        status, title = _EXCEPTION_MAP[exc_type]
        type_slug = getattr(exc, "type", "error/unknown")
        return _problem(type_slug, title, status, str(exc))

    # CouponDomainError subclasses not in the map (e.g. CouponExpired)
    if isinstance(exc, CouponDomainError):
        return _problem(exc.type, "Coupon validation failed", 422, exc.detail)

    # Generic OrderDomainError fallback
    if isinstance(exc, OrderDomainError):
        return _problem(exc.type, "Checkout error", 422, exc.detail)

    # Re-raise unexpected exceptions so Django's 500 handler catches them.
    raise exc


# ---------------------------------------------------------------------------
# Checkout view
# ---------------------------------------------------------------------------

class CheckoutView(APIView):
    """``POST /v1/carts/{cart_id}/checkout``

    Initiates the checkout flow for a cart.  Requires:
    - Request body: ``{"payment_method_id": "<uuid>", "address_id": "<uuid>"}``.
    - Header: ``Idempotency-Key: <uuid>`` (required; any UUID v1–v5).

    Returns 202 Accepted with the order and payment identifiers.

    Idempotent: repeating the same request (same key + same body) returns the
    same 202 response from the durable store without re-executing the checkout.

    ADR-NOTE: Authentication is delegated to the API gateway in production
    (PROJECT_SPEC §4.3 — bearer token validated at the edge).  This view
    trusts the tenant context set by TenantMiddleware; a dedicated auth
    middleware or permission class that verifies the bearer token and maps
    it to ``request.user`` / ``cart.user_id`` will land in a later iteration.
    """

    # ADR-NOTE: AllowAny here because the API gateway (or a future Django
    # auth middleware) enforces bearer token verification before the request
    # reaches Django views.  TenantMiddleware already scopes the data to
    # the correct tenant.  Replace with a custom permission class when the
    # bearer-token integration lands.
    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="checkout_cart",
        summary="Checkout a cart",
        description=(
            "Initiates the checkout flow for an active cart under a **distributed "
            "Redis lock** and a **Postgres row lock**, guaranteeing exactly-one "
            "order creation per cart.\n\n"
            "### Flow\n\n"
            "1. Idempotency check — returns the stored response if the key has "
            "   been seen before (without re-executing).\n"
            "2. Acquire Redis lock `lock:checkout:{tenant}:{cart}` (TTL 15s).\n"
            "3. Open Postgres transaction.\n"
            "4. `SELECT FOR UPDATE` on cart, address, and payment method.\n"
            "5. Revalidate all applied coupons.\n"
            "6. Deduct stock: `UPDATE product SET stock=stock-qty WHERE stock>=qty` "
            "   (conditional, race-safe).\n"
            "7. Create `Order` + `OrderItem` rows.\n"
            "8. Mark cart `CHECKED_OUT` with optimistic version guard.\n"
            "9. Create `Payment` in `REQUIRES_CONFIRMATION`.\n"
            "10. Persist `IdempotencyRecord` (inside the transaction — "
            "    rolls back on failure).\n"
            "11. COMMIT → `transaction.on_commit` dispatches the Celery "
            "    `authorize_payment` task.\n"
            "12. Release lock.\n\n"
            "### Concurrency guarantees\n\n"
            "- **No double orders**: Redis lock + DB row lock + unique `(tenant, "
            "  idempotency_key)` constraint make duplicate orders structurally "
            "  impossible.\n"
            "- **No oversell**: conditional stock UPDATE returns 0 rows on a race; "
            "  a `CHECK stock >= 0` DB constraint is the last resort.\n"
            "- **No orphan payment tasks**: Celery dispatch is wrapped in "
            "  `transaction.on_commit` — a rollback produces no task.\n"
            "- **Idempotent**: same `Idempotency-Key` + same body = safe retry.\n\n"
            "### Payment outcome\n\n"
            "The endpoint returns `202 Accepted` immediately. The actual gateway "
            "authorisation runs asynchronously on the Celery `payments` queue. "
            "Poll `GET /payments/{payment_id}` or listen for the webhook to "
            "determine the final outcome.\n\n"
            "> **ADR-NOTE** The payment gateway is a **mock stub** in this "
            "> iteration. Real gateway integrations (Stripe, HyperPay, Tabby) "
            "> land in a subsequent iteration."
        ),
        tags=["Checkout"],
        parameters=[
            TENANT_DOMAIN_HEADER,
            IDEMPOTENCY_KEY_HEADER,
            OpenApiParameter(
                name="cart_id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                description=(
                    "UUID of the `Cart` to check out. Must be `ACTIVE` and "
                    "owned by the authenticated customer within the current tenant."
                ),
            ),
        ],
        request=CheckoutRequestSerializer,
        responses={
            202: OpenApiResponse(
                response=CheckoutResponseSerializer,
                description=(
                    "**Checkout accepted.** Order and payment records created. "
                    "Payment authorisation is pending on the async Celery worker. "
                    "Also returned on idempotent replay — body is identical to the "
                    "original response."
                ),
                examples=checkout_response_examples(),
            ),
            400: problem_response(
                400,
                "validation/idempotency-key-required",
                "Idempotency-Key header is required or invalid",
                "Supply a UUID in the Idempotency-Key header for this endpoint.",
                description=(
                    "Returned when:\n"
                    "- `Idempotency-Key` header is absent.\n"
                    "- `Idempotency-Key` value is not a valid UUID.\n"
                    "- Request body is missing required fields or contains invalid values."
                ),
            ),
            404: problem_response(
                404,
                "cart/not-found",
                "Resource not found",
                "cart a1b2c3d4-… not found",
                description=(
                    "Returned when the `cart_id`, `address_id`, or "
                    "`payment_method_id` does not exist within the current tenant."
                ),
            ),
            409: problem_response(
                409,
                "cart/locked",
                "Conflict — cart locked or idempotency issue",
                (
                    "A concurrent checkout is already in progress for this cart. "
                    "Retry after a short back-off."
                ),
                description=(
                    "Returned in four scenarios:\n\n"
                    "| `type` | Cause | Client action |\n"
                    "|--------|-------|---------------|\n"
                    "| `cart/locked` | Redis lock already held by another process | "
                    "Retry after ~1s back-off |\n"
                    "| `cart/locked` | Cart already checked out | "
                    "Do not retry — create a new cart |\n"
                    "| `cart/stale-version` | Concurrent cart mutation | "
                    "Re-read the cart and retry |\n"
                    "| `idempotency/conflict` | Same key, different request body | "
                    "Do not retry — use a new key |\n"
                    "| `idempotency/in-progress` | Duplicate in-flight request | "
                    "Retry after short back-off |\n"
                ),
            ),
            422: problem_response(
                422,
                "product/out-of-stock",
                "Business rule violation",
                "product cccccccc-… is out of stock",
                description=(
                    "Returned when a domain invariant is violated:\n\n"
                    "| `type` | Cause |\n"
                    "|--------|-------|\n"
                    "| `cart/empty` | Cart has no items |\n"
                    "| `product/out-of-stock` | Insufficient stock for one or more items |\n"
                    "| `coupon/expired` | An applied coupon expired before checkout |\n"
                    "| `coupon/usage-limit-reached` | Coupon fully redeemed |\n"
                ),
            ),
        },
        examples=[
            *checkout_request_examples(),
            *checkout_response_examples(),
        ],
    )
    def post(self, request: Request, cart_id: uuid.UUID) -> Response:
        # ------------------------------------------------------------------
        # 1. Validate request body.
        # ------------------------------------------------------------------
        serializer = CheckoutRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"type": "validation/invalid-input", "errors": serializer.errors},
                status=400,
            )

        # ------------------------------------------------------------------
        # 2. Parse and validate Idempotency-Key header.
        # ------------------------------------------------------------------
        raw_key = request.headers.get("Idempotency-Key", "").strip()
        if not raw_key:
            return _problem(
                "validation/idempotency-key-required",
                "Idempotency-Key header is required",
                400,
                "Supply a UUID in the Idempotency-Key header for this endpoint.",
            )
        try:
            idempotency_key = uuid.UUID(raw_key)
        except ValueError:
            return _problem(
                "validation/idempotency-key-invalid",
                "Idempotency-Key must be a valid UUID",
                400,
                f"'{raw_key}' is not a valid UUID.",
            )

        # ------------------------------------------------------------------
        # 3. Compute request hash for conflict detection.
        # ------------------------------------------------------------------
        data = serializer.validated_data
        request_hash = compute_request_hash({
            "cart_id": str(cart_id),
            "payment_method_id": str(data["payment_method_id"]),
            "address_id": str(data["address_id"]),
        })

        # ------------------------------------------------------------------
        # 4. Dispatch to CheckoutService.
        # ------------------------------------------------------------------
        service = CheckoutService()
        try:
            result = service.checkout(
                cart_id=cart_id,
                payment_method_id=data["payment_method_id"],
                address_id=data["address_id"],
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
            return _map_exception(exc)

        # ------------------------------------------------------------------
        # 5. Serialise and return.
        # ------------------------------------------------------------------
        out = CheckoutResponseSerializer({
            "order_id": result.order_id,
            "payment_id": result.payment_id,
            "payment_status": result.payment_status,
            "total": result.total,
            "currency": result.currency,
        })
        return Response(out.data, status=result.http_status)
