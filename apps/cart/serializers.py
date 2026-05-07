"""Cart serializers — input validation and structured output for cart action endpoints.

Implements PROJECT_SPEC §4.1 (thin views, serializers own input validation),
§5.4 (structured REST responses), and §6.4 (input validated in serializers,
domain rules in services).

Serializers carry *no* business logic — they only validate types, required
fields, and basic format constraints.  Domain rules (stock availability,
coupon eligibility, address ownership) live in the service layer and are
raised as typed exceptions that the view maps to RFC 7807 problem+json.

Input serializers
-----------------
``AddProductSerializer``        — POST /cart/add-product
``RemoveProductSerializer``     — POST /cart/remove-product
``ApplyCouponSerializer``       — POST /cart/add-coupon
``RemoveCouponSerializer``      — POST /cart/remove-coupon
``AddAddressSerializer``        — POST /cart/add-address
``AddPaymentMethodSerializer``  — POST /cart/add-payment-method
``CartCheckoutSerializer``      — POST /cart/checkout  (empty body, header-only)

Output serializers
------------------
``CartItemSerializer``          — one line item in the cart read response
``AppliedCouponSerializer``     — one applied coupon in the cart read response
``SelectedAddressSerializer``   — snapshot of the selected address
``CartReadSerializer``          — top-level cart representation used for every
                                  POST response and the GET /cart response
"""

from __future__ import annotations

from rest_framework import serializers

from apps.catalog.models import Product


# ---------------------------------------------------------------------------
# Input serializers
# ---------------------------------------------------------------------------


class AddProductSerializer(serializers.Serializer):
    """Validate the body for ``POST /api/v1/cart/add-product``."""

    product_id = serializers.UUIDField(
        help_text="UUID of the catalog ``Product`` to add to the cart.",
    )
    quantity = serializers.IntegerField(
        min_value=1,
        help_text="Number of units to add.  Must be ≥ 1.",
    )


class RemoveProductSerializer(serializers.Serializer):
    """Validate the body for ``POST /api/v1/cart/remove-product``."""

    product_id = serializers.UUIDField(
        help_text=(
            "UUID of the catalog ``Product`` to remove.  "
            "Idempotent — removing a product not in the cart is a no-op."
        ),
    )


class ApplyCouponSerializer(serializers.Serializer):
    """Validate the body for ``POST /api/v1/cart/add-coupon``."""

    code = serializers.CharField(
        max_length=64,
        help_text="Coupon code to apply (case-sensitive, tenant-scoped).",
    )


class RemoveCouponSerializer(serializers.Serializer):
    """Validate the body for ``POST /api/v1/cart/remove-coupon``."""

    coupon_id = serializers.UUIDField(
        help_text=(
            "UUID of the ``Coupon`` to remove from the cart.  "
            "Idempotent — removing a coupon that is not applied is a no-op."
        ),
    )


class AddAddressSerializer(serializers.Serializer):
    """Validate the body for ``POST /api/v1/cart/add-address``."""

    country = serializers.RegexField(
        regex=r"^[A-Z]{2}$",
        max_length=2,
        help_text="ISO 3166-1 alpha-2 uppercase two-letter country code (e.g. ``US``).",
    )
    city = serializers.CharField(
        max_length=120,
        help_text="City name.  Must not be blank.",
    )
    details = serializers.CharField(
        help_text="Free-text address details (street line, postal code, apartment…).",
    )
    label = serializers.CharField(
        max_length=50,
        required=False,
        default="",
        help_text='Optional human-readable label, e.g. ``"home"`` or ``"office"``.',
    )
    is_default = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "If ``true``, this address becomes the customer's default and any "
            "previous default for this tenant+user is demoted."
        ),
    )

    def validate_city(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("City must not be blank.")
        return value


class AddPaymentMethodSerializer(serializers.Serializer):
    """Validate the body for ``POST /api/v1/cart/add-payment-method``."""

    gateway_slug = serializers.CharField(
        max_length=50,
        help_text=(
            "Registered gateway slug (e.g. ``dummy_success``).  "
            "The slug must exist in the gateway registry; unknown slugs return 422."
        ),
    )


class CartCheckoutSerializer(serializers.Serializer):
    """Input for ``POST /api/v1/cart/checkout``.

    The request body is intentionally empty — the address and payment method
    are resolved from the cart's ``selected_address`` and
    ``selected_payment_method`` fields (set by previous action calls).
    The ``Idempotency-Key`` header is validated by the view.
    """


# ---------------------------------------------------------------------------
# Output serializers
# ---------------------------------------------------------------------------


class CartItemSerializer(serializers.Serializer):
    """Serialise a single ``CartItem`` line in the cart read response."""

    id = serializers.UUIDField(read_only=True)
    product_id = serializers.UUIDField(read_only=True)
    quantity = serializers.IntegerField(read_only=True)
    price_snapshot = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )
    currency = serializers.CharField(read_only=True)


class AppliedCouponSerializer(serializers.Serializer):
    """Serialise a single ``CartCoupon`` entry in the cart read response."""

    id = serializers.UUIDField(read_only=True)
    coupon_id = serializers.UUIDField(read_only=True)
    code = serializers.SerializerMethodField()
    discount_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    currency = serializers.CharField(read_only=True)

    def get_code(self, obj) -> str:  # type: ignore[override]
        return obj.coupon.code


class SelectedAddressSerializer(serializers.Serializer):
    """Serialise the cart's selected shipping address (snapshot, read-only)."""

    id = serializers.UUIDField(read_only=True)
    country = serializers.CharField(read_only=True)
    city = serializers.CharField(read_only=True)
    details = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)


class SelectedPaymentMethodSerializer(serializers.Serializer):
    """Serialise the cart's selected payment method (snapshot, read-only)."""

    id = serializers.UUIDField(read_only=True)
    gateway_slug = serializers.CharField(read_only=True)


class CartReadSerializer(serializers.Serializer):
    """Top-level cart response shape used by every action endpoint and ``GET /cart``.

    Renders the full cart state — items, coupons, monetary totals, and the
    currently selected address and payment method — so the client never needs
    a follow-up read.  All fields are read-only; this serializer is output-only.
    """

    id = serializers.UUIDField(read_only=True)
    user_id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)
    currency = serializers.CharField(read_only=True)
    total_price = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True,
        help_text="Sum of (quantity × price_snapshot) before discounts.",
    )
    discount_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True,
        help_text="Total discount from all applied coupons.",
    )
    total_after_discount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True,
        help_text="Payable amount: total_price − discount_amount (clamped at 0).",
    )
    version = serializers.IntegerField(
        read_only=True,
        help_text="Optimistic concurrency token — incremented on every mutation.",
    )
    items = serializers.SerializerMethodField()
    applied_coupons = serializers.SerializerMethodField()
    selected_address = serializers.SerializerMethodField()
    selected_payment_method = serializers.SerializerMethodField()

    def get_items(self, cart) -> list:  # type: ignore[override]
        qs = cart.items.all()
        return CartItemSerializer(qs, many=True).data  # type: ignore[return-value]

    def get_applied_coupons(self, cart) -> list:  # type: ignore[override]
        qs = cart.applied_coupons.select_related("coupon").all()
        return AppliedCouponSerializer(qs, many=True).data  # type: ignore[return-value]

    def get_selected_address(self, cart):  # type: ignore[override]
        if cart.selected_address_id is None:
            return None
        addr = cart.selected_address
        return SelectedAddressSerializer(addr).data

    def get_selected_payment_method(self, cart):  # type: ignore[override]
        if cart.selected_payment_method_id is None:
            return None
        pm = cart.selected_payment_method
        return SelectedPaymentMethodSerializer(pm).data
