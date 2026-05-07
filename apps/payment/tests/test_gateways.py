"""Contract tests for every PaymentGateway implementation.

Implements PROJECT_SPEC §6.5: "100% of PaymentGateway implementations against
the shared contract."

``PaymentGatewayContractTests`` is a reusable mixin that subclasses provide
a concrete ``gateway`` instance to.  Every gateway implementation must pass
all methods in this mixin.

The three dummy gateways each subclass the mixin; their behaviour is then
verified (success / failure / timeout) by individual test methods.

No Django DB access — all tests are pure Python unit tests.
"""

from __future__ import annotations

import pytest
from decimal import Decimal

from apps.payment.exceptions import GatewayTimeout
from apps.payment.gateways.base import (
    AuthorizationResult,
    CaptureResult,
    PaymentGateway,
    RefundResult,
    VoidResult,
)
from apps.payment.gateways.dummy import (
    DummyFailingGateway,
    DummySuccessGateway,
    DummyTimeoutGateway,
)


# ---------------------------------------------------------------------------
# Shared contract mixin
# ---------------------------------------------------------------------------


class PaymentGatewayContractTests:
    """Shared contract test base.

    Subclasses must set ``gateway`` to a ``PaymentGateway`` instance and
    override ``expects_success`` if the gateway always fails / times out.

    The contract is: every method must return the correct result type (or raise
    the correct exception) and the ``PaymentGateway.slug`` must be non-empty.
    """

    gateway: PaymentGateway
    expects_success: bool = True
    expects_timeout: bool = False

    # Minimal stand-in objects — gateways must not require ORM instances.
    class _FakeOrder:
        id = "test-order-id"
        total = Decimal("100.00")
        currency = "USD"

    class _FakePaymentMethod:
        gateway_slug = "dummy"

    _order = _FakeOrder()
    _pm = _FakePaymentMethod()

    def test_slug_is_non_empty(self):
        assert self.gateway.slug, "gateway.slug must be a non-empty string"

    def test_authorize_returns_authorization_result(self):
        if self.expects_timeout:
            with pytest.raises(GatewayTimeout):
                self.gateway.authorize_payment(self._order, self._pm)
            return
        result = self.gateway.authorize_payment(self._order, self._pm)
        assert isinstance(result, AuthorizationResult)
        assert result.success is self.expects_success

    def test_authorize_success_has_reference(self):
        if self.expects_timeout or not self.expects_success:
            pytest.skip("Only applicable to success gateways")
        result = self.gateway.authorize_payment(self._order, self._pm)
        assert (
            result.reference
        ), "Successful authorization must provide a non-empty reference"

    def test_authorize_failure_has_error_code(self):
        if self.expects_timeout or self.expects_success:
            pytest.skip("Only applicable to failing gateways")
        result = self.gateway.authorize_payment(self._order, self._pm)
        assert (
            result.error_code
        ), "Failed authorization must provide a non-empty error_code"

    def test_capture_returns_capture_result(self):
        if self.expects_timeout:
            with pytest.raises(GatewayTimeout):
                self.gateway.capture_payment("any-ref")
            return
        result = self.gateway.capture_payment("any-ref")
        assert isinstance(result, CaptureResult)
        assert result.success is self.expects_success

    def test_void_returns_void_result(self):
        if self.expects_timeout:
            with pytest.raises(GatewayTimeout):
                self.gateway.void_payment("any-ref")
            return
        result = self.gateway.void_payment("any-ref")
        assert isinstance(result, VoidResult)
        assert result.success is self.expects_success

    def test_refund_returns_refund_result(self):
        if self.expects_timeout:
            with pytest.raises(GatewayTimeout):
                self.gateway.refund_payment("any-ref")
            return
        result = self.gateway.refund_payment("any-ref", amount=Decimal("10.00"))
        assert isinstance(result, RefundResult)
        assert result.success is self.expects_success

    def test_refund_without_amount_is_accepted(self):
        if self.expects_timeout:
            pytest.skip("Timeout gateway raises immediately")
        result = self.gateway.refund_payment("any-ref")
        assert isinstance(result, RefundResult)

    def test_authorize_accepts_metadata(self):
        """Gateway must accept an optional ``metadata`` dict and not blow up."""
        if self.expects_timeout:
            with pytest.raises(GatewayTimeout):
                self.gateway.authorize_payment(
                    self._order, self._pm, metadata={"ref": "abc"}
                )
            return
        result = self.gateway.authorize_payment(
            self._order, self._pm, metadata={"merchant_ref": "ORDER-42"}
        )
        assert isinstance(result, AuthorizationResult)


# ---------------------------------------------------------------------------
# Concrete contract tests — one class per gateway
# ---------------------------------------------------------------------------


class TestDummySuccessGatewayContract(PaymentGatewayContractTests):
    gateway = DummySuccessGateway()
    expects_success = True
    expects_timeout = False

    def test_successive_authorize_calls_return_different_references(self):
        """Each authorize call must return a fresh synthetic reference."""
        r1 = self.gateway.authorize_payment(self._order, self._pm)
        r2 = self.gateway.authorize_payment(self._order, self._pm)
        assert r1.reference != r2.reference


class TestDummyFailingGatewayContract(PaymentGatewayContractTests):
    gateway = DummyFailingGateway()
    expects_success = False
    expects_timeout = False

    def test_error_code_is_card_declined(self):
        result = self.gateway.authorize_payment(self._order, self._pm)
        assert result.error_code == "card_declined"

    def test_capture_error_code_is_card_declined(self):
        result = self.gateway.capture_payment("any-ref")
        assert result.error_code == "card_declined"


class TestDummyTimeoutGatewayContract(PaymentGatewayContractTests):
    gateway = DummyTimeoutGateway()
    expects_success = False
    expects_timeout = True

    def test_timeout_is_gateway_timeout_subclass(self):
        from apps.payment.exceptions import PaymentDomainError

        with pytest.raises(GatewayTimeout) as exc_info:
            self.gateway.authorize_payment(self._order, self._pm)
        assert isinstance(exc_info.value, PaymentDomainError)
