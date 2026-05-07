"""Deterministic dummy gateway implementations.

Implements PROJECT_SPEC §3.3 / §8: "A ``MockGateway`` and a ``SandboxGateway``
(deterministic, configurable success/failure) are sufficient for end-to-end
tests."

Three dummy gateways are provided:

``DummySuccessGateway`` (slug ``"dummy_success"``)
    Every method returns a successful result with a synthetic reference of the
    form ``"dummy-<operation>-<uuid4>"``.  Used for the default happy-path in
    tests and local development.

``DummyFailingGateway`` (slug ``"dummy_failing"``)
    Every method returns a failure result with ``error_code="card_declined"``.
    Used to exercise the FAILED payment path and the ``GatewayDeclined``
    handling in ``PaymentService``.

``DummyTimeoutGateway`` (slug ``"dummy_timeout"``)
    Every method raises ``GatewayTimeout``.  Used to verify that Celery
    retries are scheduled correctly and that the ``Payment`` row remains in its
    pre-call state.

All three are auto-registered when this module is imported (which happens in
``PaymentConfig.ready()``).  Tests that need to reset the registry between runs
should use the ``register_dummies`` fixture in
``apps/payment/tests/conftest.py``.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Mapping

from apps.payment.exceptions import GatewayTimeout
from apps.payment.gateways.base import (
    AuthorizationResult,
    CaptureResult,
    ChargeResult,
    PaymentGateway,
    RefundResult,
    VoidResult,
)
from apps.payment.gateways.registry import register_payment_gateway

logger = logging.getLogger(__name__)


class DummySuccessGateway(PaymentGateway):
    """Always-successful gateway for tests and local development.

    Returns deterministic synthetic references — no network calls made.
    """

    slug = "dummy_success"

    def authorize_payment(
        self,
        order: Any,
        payment_method: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorizationResult:
        ref = f"dummy-auth-{uuid.uuid4().hex}"
        logger.debug(
            "DummySuccessGateway.authorize_payment: order=%s ref=%s",
            getattr(order, "id", order),
            ref,
        )
        return AuthorizationResult(success=True, reference=ref)

    def capture_payment(self, payment_reference: str) -> CaptureResult:
        ref = f"dummy-capture-{uuid.uuid4().hex}"
        return CaptureResult(success=True, reference=ref)

    def void_payment(self, payment_reference: str) -> VoidResult:
        ref = f"dummy-void-{uuid.uuid4().hex}"
        return VoidResult(success=True, reference=ref)

    def refund_payment(
        self,
        payment_reference: str,
        amount: Decimal | None = None,
    ) -> RefundResult:
        ref = f"dummy-refund-{uuid.uuid4().hex}"
        return RefundResult(
            success=True,
            reference=ref,
            refunded_amount=amount if amount is not None else Decimal("0"),
        )


class DummyFailingGateway(PaymentGateway):
    """Always-declining gateway for testing the failed-payment path.

    Returns ``success=False`` with ``error_code="card_declined"`` for every
    operation.  ``PaymentService`` catches this and transitions the ``Payment``
    to ``FAILED``.
    """

    slug = "dummy_failing"

    def authorize_payment(
        self,
        order: Any,
        payment_method: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorizationResult:
        logger.debug(
            "DummyFailingGateway.authorize_payment: declining order=%s",
            getattr(order, "id", order),
        )
        return AuthorizationResult(
            success=False,
            error_code="card_declined",
            error_message="Your card was declined.",
        )

    def capture_payment(self, payment_reference: str) -> CaptureResult:
        return CaptureResult(
            success=False,
            error_code="card_declined",
            error_message="Your card was declined.",
        )

    def void_payment(self, payment_reference: str) -> VoidResult:
        return VoidResult(
            success=False,
            error_code="card_declined",
            error_message="Your card was declined.",
        )

    def refund_payment(
        self,
        payment_reference: str,
        amount: Decimal | None = None,
    ) -> RefundResult:
        return RefundResult(
            success=False,
            error_code="card_declined",
            error_message="Your card was declined.",
        )


class DummyTimeoutGateway(PaymentGateway):
    """Always-timing-out gateway for testing Celery retry behaviour.

    Raises ``GatewayTimeout`` on every call, leaving the ``Payment`` row in
    its pre-call state so the Celery task's ``autoretry_for`` mechanism picks
    it up and reschedules.
    """

    slug = "dummy_timeout"

    def authorize_payment(
        self,
        order: Any,
        payment_method: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorizationResult:
        raise GatewayTimeout("DummyTimeoutGateway: simulated authorization timeout")

    def capture_payment(self, payment_reference: str) -> CaptureResult:
        raise GatewayTimeout("DummyTimeoutGateway: simulated capture timeout")

    def void_payment(self, payment_reference: str) -> VoidResult:
        raise GatewayTimeout("DummyTimeoutGateway: simulated void timeout")

    def refund_payment(
        self,
        payment_reference: str,
        amount: Decimal | None = None,
    ) -> RefundResult:
        raise GatewayTimeout("DummyTimeoutGateway: simulated refund timeout")


class DummyGateway(PaymentGateway):
    """Configurable, deterministic gateway for the simple ``charge()`` facade.

    Behaviour is controlled entirely via the ``data`` dict passed to
    ``charge()``:

    - ``data={}`` or ``data=None``               → success with a synthetic reference.
    - ``data={"decline": True}``                 → failure with ``error_code="card_declined"``.
    - ``data={"decline": True, "error_code": X}``→ failure with custom code.

    The four FSM methods (authorize/capture/void/refund) are implemented as
    always-successful stubs so this gateway satisfies the ``PaymentGateway``
    ABC and can be used anywhere the FSM interface is required.
    """

    slug = "dummy"

    def charge(
        self,
        amount: Decimal,
        currency: str,
        data: Mapping[str, Any] | None = None,
    ) -> ChargeResult:
        data = dict(data or {})
        if data.get("decline"):
            return ChargeResult(
                success=False,
                amount=Decimal(amount),
                currency=currency,
                error_code=data.get("error_code", "card_declined"),
                error_message=data.get("error_message", "Your card was declined."),
            )
        ref = f"dummy-charge-{uuid.uuid4().hex}"
        logger.debug("DummyGateway.charge: ref=%s amount=%s%s", ref, amount, currency)
        return ChargeResult(
            success=True,
            reference=ref,
            amount=Decimal(amount),
            currency=currency,
            raw={"data": data},
        )

    def authorize_payment(
        self,
        order: Any,
        payment_method: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorizationResult:
        return AuthorizationResult(
            success=True, reference=f"dummy-auth-{uuid.uuid4().hex}"
        )

    def capture_payment(self, payment_reference: str) -> CaptureResult:
        return CaptureResult(
            success=True, reference=f"dummy-capture-{uuid.uuid4().hex}"
        )

    def void_payment(self, payment_reference: str) -> VoidResult:
        return VoidResult(success=True, reference=f"dummy-void-{uuid.uuid4().hex}")

    def refund_payment(
        self,
        payment_reference: str,
        amount: Decimal | None = None,
    ) -> RefundResult:
        return RefundResult(
            success=True,
            reference=f"dummy-refund-{uuid.uuid4().hex}",
            refunded_amount=Decimal(amount) if amount is not None else Decimal("0"),
        )


# ---------------------------------------------------------------------------
# Auto-registration — runs when this module is first imported (in PaymentConfig.ready())
# ---------------------------------------------------------------------------

register_payment_gateway(DummySuccessGateway.slug, DummySuccessGateway)
register_payment_gateway(DummyFailingGateway.slug, DummyFailingGateway)
register_payment_gateway(DummyTimeoutGateway.slug, DummyTimeoutGateway)
# ADR-NOTE: "mock" is retained as an alias for DummySuccessGateway to avoid
# breaking any external tooling or fixtures that may reference the old slug
# before all callers are migrated to "dummy_success".
register_payment_gateway("mock", DummySuccessGateway)
register_payment_gateway(DummyGateway.slug, DummyGateway)
