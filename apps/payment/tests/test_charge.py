"""Tests for the charge() facade: ChargeResult, DummyGateway, registry aliases,
PaymentService.process_payment, and third-party extensibility.

All tests are pure Python (no DB) — no ``@pytest.mark.django_db`` needed.
The ``register_dummies`` autouse fixture in conftest.py keeps the registry
clean between runs and pre-registers the "dummy" slug.
"""

from __future__ import annotations

import pytest
from decimal import Decimal
from typing import Any, Mapping

from apps.payment.exceptions import UnsupportedGateway
from apps.payment.gateways.base import (
    AuthorizationResult,
    CaptureResult,
    ChargeResult,
    PaymentGateway,
    RefundResult,
    VoidResult,
)
from apps.payment.gateways.dummy import DummyGateway, DummySuccessGateway
from apps.payment.gateways.registry import (
    _REGISTRY,
    get_gateway,
    get_payment_gateway,
    register_gateway,
    register_payment_gateway,
    unregister_payment_gateway,
)
from apps.payment.services import PaymentService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLUGIN_SLUG = "_charge_test_plugin"


@pytest.fixture(autouse=True)
def clean_plugin_slug():
    """Ensure the plugin test slug is absent before and after each test."""
    if _PLUGIN_SLUG in _REGISTRY:
        unregister_payment_gateway(_PLUGIN_SLUG)
    yield
    if _PLUGIN_SLUG in _REGISTRY:
        unregister_payment_gateway(_PLUGIN_SLUG)


class _PluginGateway(PaymentGateway):
    """Minimal third-party gateway stub used in extensibility tests."""

    slug = _PLUGIN_SLUG

    def charge(
        self,
        amount: Decimal,
        currency: str,
        data: Mapping[str, Any] | None = None,
    ) -> ChargeResult:
        return ChargeResult(
            success=True,
            reference="plugin-ref-001",
            amount=Decimal(amount),
            currency=currency,
            raw={"source": "plugin"},
        )

    def authorize_payment(self, order, payment_method, metadata=None):
        return AuthorizationResult(success=True, reference="plugin-auth")

    def capture_payment(self, payment_reference):
        return CaptureResult(success=True, reference="plugin-capture")

    def void_payment(self, payment_reference):
        return VoidResult(success=True, reference="plugin-void")

    def refund_payment(self, payment_reference, amount=None):
        return RefundResult(success=True, reference="plugin-refund")


# ---------------------------------------------------------------------------
# ChargeResult dataclass contract
# ---------------------------------------------------------------------------


def test_charge_result_is_frozen():
    result = ChargeResult(success=True, reference="ref-1", amount=Decimal("10"), currency="USD")
    with pytest.raises((AttributeError, TypeError)):
        result.success = False  # type: ignore[misc]


def test_charge_result_default_fields():
    result = ChargeResult(success=True)
    assert result.reference == ""
    assert result.amount == Decimal("0")
    assert result.currency == ""
    assert result.error_code == ""
    assert result.error_message == ""
    assert result.raw == {}


def test_charge_result_carries_amount_and_currency():
    result = ChargeResult(success=True, amount=Decimal("42.50"), currency="EUR")
    assert result.amount == Decimal("42.50")
    assert result.currency == "EUR"


# ---------------------------------------------------------------------------
# DummyGateway — charge() behaviour
# ---------------------------------------------------------------------------


def test_dummy_gateway_charge_success():
    gw = DummyGateway()
    result = gw.charge(Decimal("10.00"), "USD")

    assert result.success is True
    assert result.reference.startswith("dummy-charge-")
    assert result.amount == Decimal("10.00")
    assert result.currency == "USD"


def test_dummy_gateway_charge_success_with_empty_data():
    gw = DummyGateway()
    result = gw.charge(Decimal("5.00"), "GBP", {})

    assert result.success is True
    assert result.currency == "GBP"


def test_dummy_gateway_charge_decline_flag():
    gw = DummyGateway()
    result = gw.charge(Decimal("20.00"), "USD", {"decline": True})

    assert result.success is False
    assert result.error_code == "card_declined"
    assert result.error_message != ""
    assert result.reference == ""


def test_dummy_gateway_charge_custom_error_code():
    gw = DummyGateway()
    result = gw.charge(
        Decimal("20.00"),
        "USD",
        {"decline": True, "error_code": "insufficient_funds", "error_message": "Not enough funds."},
    )

    assert result.success is False
    assert result.error_code == "insufficient_funds"
    assert result.error_message == "Not enough funds."


def test_dummy_gateway_successive_charges_have_unique_references():
    gw = DummyGateway()
    r1 = gw.charge(Decimal("1.00"), "USD")
    r2 = gw.charge(Decimal("1.00"), "USD")
    assert r1.reference != r2.reference


def test_dummy_gateway_slug():
    assert DummyGateway.slug == "dummy"


# ---------------------------------------------------------------------------
# Default ABC charge() delegates to authorize_payment (backward-compat)
# ---------------------------------------------------------------------------


def test_existing_gateway_charge_default_delegates_to_authorize():
    """DummySuccessGateway (no override) must succeed via the ABC default."""
    gw = DummySuccessGateway()
    result = gw.charge(Decimal("30.00"), "USD", {"order_id": "test-123"})

    assert isinstance(result, ChargeResult)
    assert result.success is True
    assert result.reference.startswith("dummy-auth-")
    assert result.amount == Decimal("30.00")
    assert result.currency == "USD"


# ---------------------------------------------------------------------------
# Registry aliases
# ---------------------------------------------------------------------------


def test_register_gateway_is_alias_for_register_payment_gateway():
    assert register_gateway is register_payment_gateway


def test_get_gateway_is_alias_for_get_payment_gateway():
    assert get_gateway is get_payment_gateway


def test_register_gateway_alias_round_trip():
    register_gateway(_PLUGIN_SLUG, _PluginGateway)
    gw = get_gateway(_PLUGIN_SLUG)
    assert isinstance(gw, _PluginGateway)


def test_get_gateway_returns_singleton():
    register_gateway(_PLUGIN_SLUG, _PluginGateway)
    gw1 = get_gateway(_PLUGIN_SLUG)
    gw2 = get_gateway(_PLUGIN_SLUG)
    assert gw1 is gw2


def test_get_gateway_unknown_slug_raises_unsupported_gateway():
    with pytest.raises(UnsupportedGateway) as exc_info:
        get_gateway("__not_registered__")
    assert exc_info.value.slug == "__not_registered__"


# ---------------------------------------------------------------------------
# PaymentService.process_payment
# ---------------------------------------------------------------------------


def test_process_payment_success():
    result = PaymentService().process_payment("dummy", Decimal("25.00"), "USD", {})

    assert isinstance(result, ChargeResult)
    assert result.success is True
    assert result.reference.startswith("dummy-charge-")
    assert result.amount == Decimal("25.00")
    assert result.currency == "USD"


def test_process_payment_decline():
    result = PaymentService().process_payment(
        "dummy", Decimal("25.00"), "USD", {"decline": True}
    )

    assert result.success is False
    assert result.error_code == "card_declined"


def test_process_payment_custom_decline_code():
    result = PaymentService().process_payment(
        "dummy",
        Decimal("50.00"),
        "EUR",
        {"decline": True, "error_code": "do_not_honor"},
    )

    assert result.success is False
    assert result.error_code == "do_not_honor"


def test_process_payment_unsupported_gateway_raises():
    with pytest.raises(UnsupportedGateway):
        PaymentService().process_payment("nonexistent_gw", Decimal("10.00"), "USD")


def test_process_payment_none_data_is_accepted():
    """Omitting data should work identically to passing an empty dict."""
    result = PaymentService().process_payment("dummy", Decimal("1.00"), "USD")
    assert result.success is True


# ---------------------------------------------------------------------------
# Extensibility: register a third-party gateway without modifying core logic
# ---------------------------------------------------------------------------


def test_extensibility_third_party_gateway():
    """A new gateway registered via register_gateway is immediately callable
    through PaymentService.process_payment with no changes to core logic.
    """
    register_gateway(_PLUGIN_SLUG, _PluginGateway)

    result = PaymentService().process_payment(_PLUGIN_SLUG, Decimal("99.99"), "SAR")

    assert result.success is True
    assert result.reference == "plugin-ref-001"
    assert result.raw == {"source": "plugin"}


def test_extensibility_gateway_overrides_charge_independently():
    """A third-party gateway can implement charge() independently of the
    authorize/capture FSM and still work transparently through the service.
    """
    register_gateway(_PLUGIN_SLUG, _PluginGateway)

    gw = get_gateway(_PLUGIN_SLUG)
    result = gw.charge(Decimal("1.00"), "USD")

    assert isinstance(result, ChargeResult)
    assert result.success is True
