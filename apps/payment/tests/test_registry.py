"""Tests for the payment gateway registry.

Covers:
- register / get round-trip.
- ``get_payment_gateway`` raises ``UnsupportedGateway`` for unknown slugs.
- Double-registration on the same slug raises ``ValueError``.
- ``registered_gateways()`` returns a sorted list of slugs.
- ``unregister_payment_gateway`` removes the slug.
- ``unregister_payment_gateway`` raises ``KeyError`` for unknown slugs.

No Django DB access — pure Python tests.
"""

from __future__ import annotations

import pytest

from apps.payment.exceptions import UnsupportedGateway
from apps.payment.gateways.base import (
    AuthorizationResult,
    CaptureResult,
    PaymentGateway,
    RefundResult,
    VoidResult,
)
from apps.payment.gateways.registry import (
    _REGISTRY,
    get_payment_gateway,
    register_payment_gateway,
    registered_gateways,
    unregister_payment_gateway,
)


# ---------------------------------------------------------------------------
# Helper gateway for registry tests
# ---------------------------------------------------------------------------


class _RegistryTestGateway(PaymentGateway):
    """Minimal stub gateway used by registry tests only."""

    slug = "_registry_test_slug"

    def authorize_payment(self, order, payment_method, metadata=None):
        return AuthorizationResult(success=True, reference="ok")

    def capture_payment(self, payment_reference):
        return CaptureResult(success=True, reference="ok")

    def void_payment(self, payment_reference):
        return VoidResult(success=True, reference="ok")

    def refund_payment(self, payment_reference, amount=None):
        return RefundResult(success=True, reference="ok")


_TEST_SLUG = "_registry_test_slug"


@pytest.fixture(autouse=True)
def clean_test_slug():
    """Ensure the test slug is absent before and after each registry test."""
    if _TEST_SLUG in _REGISTRY:
        unregister_payment_gateway(_TEST_SLUG)
    yield
    if _TEST_SLUG in _REGISTRY:
        unregister_payment_gateway(_TEST_SLUG)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_and_get_round_trip():
    """register then get returns an instance of the gateway class."""
    register_payment_gateway(_TEST_SLUG, _RegistryTestGateway)
    gw = get_payment_gateway(_TEST_SLUG)
    assert isinstance(gw, _RegistryTestGateway)


def test_get_unsupported_gateway_raises_unsupported_gateway():
    """get_payment_gateway for an unknown slug raises UnsupportedGateway."""
    with pytest.raises(UnsupportedGateway) as exc_info:
        get_payment_gateway("definitely_not_registered")
    assert exc_info.value.slug == "definitely_not_registered"
    assert exc_info.value.type == "payment/gateway-unavailable"


def test_double_registration_raises_value_error():
    """Registering the same slug twice raises ValueError (fail loud)."""
    register_payment_gateway(_TEST_SLUG, _RegistryTestGateway)
    with pytest.raises(ValueError, match="already registered"):
        register_payment_gateway(_TEST_SLUG, _RegistryTestGateway)


def test_register_empty_slug_raises_value_error():
    """Registering with an empty slug raises ValueError."""
    with pytest.raises(ValueError):
        register_payment_gateway("", _RegistryTestGateway)


def test_unregister_removes_slug():
    """unregister_payment_gateway removes the slug from the registry."""
    register_payment_gateway(_TEST_SLUG, _RegistryTestGateway)
    unregister_payment_gateway(_TEST_SLUG)
    with pytest.raises(UnsupportedGateway):
        get_payment_gateway(_TEST_SLUG)


def test_unregister_unknown_slug_raises_key_error():
    """unregister_payment_gateway raises KeyError for an unknown slug."""
    with pytest.raises(KeyError):
        unregister_payment_gateway("not_registered_at_all")


def test_registered_gateways_includes_dummies():
    """registered_gateways() lists at least the four dummy slugs."""
    slugs = registered_gateways()
    assert "dummy_success" in slugs
    assert "dummy_failing" in slugs
    assert "dummy_timeout" in slugs
    assert "mock" in slugs


def test_registered_gateways_is_sorted():
    """registered_gateways() returns a sorted list."""
    slugs = registered_gateways()
    assert slugs == sorted(slugs)


def test_get_gateway_returns_same_instance_on_repeated_calls():
    """The registry is a singleton pool — repeated gets return the same object."""
    register_payment_gateway(_TEST_SLUG, _RegistryTestGateway)
    gw1 = get_payment_gateway(_TEST_SLUG)
    gw2 = get_payment_gateway(_TEST_SLUG)
    assert gw1 is gw2
