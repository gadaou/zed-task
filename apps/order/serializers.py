"""Order serializers — input validation and response serialisation.

Implements PROJECT_SPEC §4.1 (thin views, serializers handle input shape),
§5.4 (REST API design), and §6.4 (validation in serializers, domain rules
in services).

Serializers carry *no* business logic — they only validate types, required
fields, and basic format constraints.  Domain rules (stock, idempotency,
coupon eligibility) live in ``CheckoutService``.

OpenAPI documentation is driven by drf-spectacular.  Every field carries a
``help_text`` that appears in Swagger UI, and each serializer declares
``Meta.openapi_extra`` so the generated schema includes the correct
``examples`` block alongside the ``properties``.
"""

from __future__ import annotations

from rest_framework import serializers


class CheckoutRequestSerializer(serializers.Serializer):
    """Input for ``POST /v1/carts/{cart_id}/checkout``.

    The ``Idempotency-Key`` header is validated by the view, not here, because
    DRF serializers operate on the request body, not headers.
    """

    payment_method_id = serializers.UUIDField(
        help_text=(
            "UUID of the `PaymentMethod` to charge. The payment method must "
            "belong to the active tenant and be in a chargeable state."
        ),
    )
    address_id = serializers.UUIDField(
        help_text=(
            "UUID of the shipping `Address`. The address must belong to the "
            "same `user_id` as the cart and must not be soft-deleted."
        ),
    )

    class Meta:
        # Extra schema properties injected verbatim into the generated OpenAPI
        # component.  The ``examples`` block makes Swagger UI show a 'try it'
        # pre-filled example without any click required.
        openapi_extra = {
            "examples": {
                "StandardCheckout": {
                    "summary": "Standard checkout",
                    "description": (
                        "Typical first-time checkout: an active cart with items, a "
                        "stored payment method, and a shipping address owned by the "
                        "same customer."
                    ),
                    "value": {
                        "payment_method_id": "11111111-0000-0000-0000-000000000001",
                        "address_id": "22222222-0000-0000-0000-000000000002",
                    },
                },
                "IdempotentRetry": {
                    "summary": "Idempotent retry",
                    "description": (
                        "Re-send the exact same body with the **same** `Idempotency-Key` "
                        "header.  The server returns the original `202` without creating "
                        "a second order."
                    ),
                    "value": {
                        "payment_method_id": "11111111-0000-0000-0000-000000000001",
                        "address_id": "22222222-0000-0000-0000-000000000002",
                    },
                },
            }
        }


class CheckoutResponseSerializer(serializers.Serializer):
    """Response body for a successful checkout (HTTP 202 Accepted).

    Fields mirror the ``CheckoutResult`` dataclass and the payload stored in
    ``IdempotencyRecord.response_body``.  Both original responses and
    idempotent replays use this serializer so the client always receives an
    identical shape.
    """

    order_id = serializers.UUIDField(
        read_only=True,
        help_text=(
            "UUID of the newly created `Order`. Stable — use this to poll "
            "order status or list order items."
        ),
    )
    payment_id = serializers.UUIDField(
        read_only=True,
        help_text=(
            "UUID of the `Payment` record created for this checkout. "
            "Poll this to track the authorisation state "
            "(`REQUIRES_CONFIRMATION → AUTHORIZED → CAPTURED → SUCCEEDED`)."
        ),
    )
    payment_status = serializers.CharField(
        read_only=True,
        help_text=(
            "`pending` — the payment authorisation is being processed "
            "asynchronously by the Celery `payments` worker. "
            "`authorized` — the gateway confirmed the charge inline "
            "(only for synchronous gateways; not used in the current iteration)."
        ),
    )
    total = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        read_only=True,
        help_text=(
            "Amount charged (or to be charged) in `currency` units. "
            "This is `cart.total_after_discount` snapshotted at checkout time; "
            "it does not change after the order is created."
        ),
    )
    currency = serializers.CharField(
        max_length=3,
        read_only=True,
        help_text=(
            "ISO 4217 three-letter currency code inherited from the cart "
            "(e.g. `USD`, `EUR`, `SAR`)."
        ),
    )

    class Meta:
        openapi_extra = {
            "examples": {
                "CheckoutAccepted": {
                    "summary": "202 — Checkout accepted",
                    "description": (
                        "Order created and committed to the database. Payment "
                        "authorisation is in progress asynchronously."
                    ),
                    "value": {
                        "order_id": "aaaaaaaa-0000-0000-0000-000000000001",
                        "payment_id": "bbbbbbbb-0000-0000-0000-000000000002",
                        "payment_status": "pending",
                        "total": "149.99",
                        "currency": "USD",
                    },
                },
                "IdempotentReplay": {
                    "summary": "202 — Idempotent replay",
                    "description": (
                        "The server detected a duplicate request via the "
                        "`Idempotency-Key` and returned the stored response. "
                        "No new order or payment was created."
                    ),
                    "value": {
                        "order_id": "aaaaaaaa-0000-0000-0000-000000000001",
                        "payment_id": "bbbbbbbb-0000-0000-0000-000000000002",
                        "payment_status": "pending",
                        "total": "149.99",
                        "currency": "USD",
                    },
                },
            }
        }
