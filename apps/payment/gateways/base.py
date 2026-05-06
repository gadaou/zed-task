"""PaymentGateway abstract base class and result dataclasses.

Implements PROJECT_SPEC §3.3 (pluggable gateway interface).

Every concrete gateway must:
1. Subclass ``PaymentGateway``.
2. Set the class-level ``slug`` attribute to a unique lowercase slug (e.g.
   ``"stripe"``, ``"dummy_success"``).
3. Implement all four abstract methods.
4. Register itself via ``register_payment_gateway`` (typically at import time
   inside the gateway's module, so that importing the module is sufficient).

The interface deliberately uses plain Python dataclasses for method arguments
and return values rather than Django model instances — this keeps the gateway
layer decoupled from the ORM and makes unit testing without a DB trivial.

ADR-NOTE — ``webhook_verify`` is deliberately absent:
    PROJECT_SPEC §3.3 shows it in the illustrative interface, but webhook
    ingestion ships in the checkout-completion iteration.  The four methods
    below cover the async-authorization flow specified in §4.6.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Result dataclasses (immutable value objects returned by every gateway call)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationResult:
    """Outcome of a ``PaymentGateway.authorize_payment`` call.

    On success ``success=True`` and ``reference`` holds the gateway-side
    authorization identifier (passed to subsequent capture/void calls).
    On failure ``success=False``, ``error_code`` carries the machine-readable
    reason, and ``error_message`` carries the human-readable detail.
    """

    success: bool
    reference: str = ""
    error_code: str = ""
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of a ``PaymentGateway.capture_payment`` call."""

    success: bool
    reference: str = ""
    error_code: str = ""
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoidResult:
    """Outcome of a ``PaymentGateway.void_payment`` call."""

    success: bool
    reference: str = ""
    error_code: str = ""
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RefundResult:
    """Outcome of a ``PaymentGateway.refund_payment`` call."""

    success: bool
    reference: str = ""
    refunded_amount: Decimal = Decimal("0")
    error_code: str = ""
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class PaymentGateway(abc.ABC):
    """Abstract base for every payment gateway integration.

    Concrete gateways subclass this, set ``slug``, and implement the four
    abstract methods.  The gateway must be stateless (no per-request instance
    state) so a single instance can be safely shared across threads.

    Gateway implementations must NEVER:
    - Log PII (full card numbers, CVV, full account numbers).
    - Import Django ORM models — that is the service layer's job.
    - Mutate ``Payment`` rows directly.

    The service layer (``PaymentService``) is responsible for:
    - Loading / storing ORM rows.
    - FSM transition guards (idempotency).
    - ``transaction.atomic()`` boundaries.

    Usage::

        gateway = get_payment_gateway("stripe")
        result = gateway.authorize_payment(order, payment_method)
        if result.success:
            ...  # service persists AUTHORIZED + gateway_authorization_id
        else:
            ...  # service persists FAILED + failure_reason
    """

    #: Unique lowercase slug used to look up this gateway in the registry.
    #: Must be set on every concrete subclass.
    slug: str = ""

    @abc.abstractmethod
    def authorize_payment(
        self,
        order: Any,
        payment_method: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorizationResult:
        """Attempt to authorise the charge for ``order``.

        Args:
            order:          The ``Order`` ORM instance (authorisation amount
                            and currency are read from ``order.total`` and
                            ``order.currency``).
            payment_method: The ``PaymentMethod`` ORM instance (provides
                            ``gateway_slug`` and any tokenised instrument data).
            metadata:       Optional free-form dict forwarded to the gateway
                            as order-level metadata (e.g. merchant reference).

        Returns:
            ``AuthorizationResult`` with ``success=True`` and a non-empty
            ``reference`` on success, or ``success=False`` and populated
            ``error_code``/``error_message`` on decline.

        Raises:
            ``GatewayTimeout``:      transient — Celery should retry.
            ``GatewayUnavailable``:  transient — Celery should retry.
            Any other exception is a programming error and will NOT be retried.
        """

    @abc.abstractmethod
    def capture_payment(self, payment_reference: str) -> CaptureResult:
        """Capture a previously authorised charge.

        Args:
            payment_reference: The ``reference`` value from the corresponding
                               ``AuthorizationResult``.

        Returns:
            ``CaptureResult`` with ``success=True`` and a capture-side
            ``reference`` on success.
        """

    @abc.abstractmethod
    def void_payment(self, payment_reference: str) -> VoidResult:
        """Void (cancel) a previously authorised charge before capture.

        Args:
            payment_reference: The ``reference`` from ``AuthorizationResult``.

        Returns:
            ``VoidResult`` with ``success=True`` when the void was accepted.
        """

    @abc.abstractmethod
    def refund_payment(
        self,
        payment_reference: str,
        amount: Decimal | None = None,
    ) -> RefundResult:
        """Issue a full or partial refund against a captured charge.

        Args:
            payment_reference: The ``reference`` from ``CaptureResult``.
            amount:            Optional; if omitted the gateway issues a full
                               refund.

        Returns:
            ``RefundResult`` with ``success=True`` and ``refunded_amount`` set
            to the actual amount refunded.
        """
