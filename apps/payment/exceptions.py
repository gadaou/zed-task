"""Payment domain exceptions.

Implements the payment-related entries of the PROJECT_SPEC §2 error taxonomy:

    payment/declined            — gateway declined the charge.
    payment/gateway-unavailable — gateway is unreachable or timed out.

Design rules (mirrors apps/order/exceptions.py):
- Every exception inherits from ``PaymentDomainError`` for uniform ``except``
  coverage in the view / task layers.
- ``type`` matches the spec §2 taxonomy verbatim.
- ``detail`` is safe-for-logs; it must never carry PII or card numbers.
"""

from __future__ import annotations

from typing import Any


class PaymentDomainError(Exception):
    """Base class for every payment domain error."""

    type: str = "payment/error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        self.detail = detail or self.__class__.__doc__ or ""
        self.extra = extra
        super().__init__(self.detail)


class UnsupportedGateway(PaymentDomainError):
    """The requested payment gateway slug is not registered.

    Maps to ``payment/gateway-unavailable`` in the PROJECT_SPEC §2 taxonomy.
    The slug was never registered (or was mis-spelled), which is a permanent
    configuration error distinct from a transient gateway outage.
    """

    type = "payment/gateway-unavailable"

    def __init__(self, slug: str = "", detail: str = "", **extra: Any) -> None:
        self.slug = slug
        super().__init__(
            detail
            or (
                f"payment gateway '{slug}' is not registered"
                if slug
                else "payment gateway is not registered"
            ),
            slug=slug,
            **extra,
        )


class GatewayDeclined(PaymentDomainError):
    """The payment gateway declined the charge.

    Terminal failure — the PaymentService persists ``Payment.status = FAILED``
    before raising this, so callers do not need to update the row again.
    The ``error_code`` is a short machine-readable string from the gateway
    (e.g. ``"card_declined"``, ``"insufficient_funds"``).
    """

    type = "payment/declined"

    def __init__(
        self,
        error_code: str = "",
        detail: str = "",
        **extra: Any,
    ) -> None:
        self.error_code = error_code
        super().__init__(
            detail
            or (
                f"gateway declined with code '{error_code}'"
                if error_code
                else "gateway declined the charge"
            ),
            error_code=error_code,
            **extra,
        )


class GatewayTimeout(PaymentDomainError):
    """The payment gateway call timed out.

    Transient failure — signals the Celery task to schedule a retry.  The
    ``Payment`` row is left in ``REQUIRES_CONFIRMATION`` so the task can safely
    pick it up again.  The ``autoretry_for`` decorator on
    ``authorize_payment`` catches this class and re-schedules with back-off.
    """

    type = "payment/gateway-unavailable"


class GatewayUnavailable(PaymentDomainError):
    """The payment gateway is unavailable (network error, 5xx response, etc.).

    Transient failure, same Celery-retry semantics as ``GatewayTimeout``.
    This class replaces the bare ``GatewayUnavailable`` that previously lived
    in ``apps/payment/tasks.py``; that definition now imports from here.
    """

    type = "payment/gateway-unavailable"
