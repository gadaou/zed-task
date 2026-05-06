"""Payment gateways package — public surface.

Import everything you need from here::

    from apps.payment.gateways import (
        PaymentGateway,
        AuthorizationResult, CaptureResult, VoidResult, RefundResult,
        register_payment_gateway, get_payment_gateway,
        DummySuccessGateway, DummyFailingGateway, DummyTimeoutGateway,
    )

The dummy gateway classes are imported lazily (not at module load) to avoid
triggering auto-registration before ``PaymentConfig.ready()`` runs.  Use the
registry functions to look up instances at call time.
"""

from apps.payment.gateways.base import (
    AuthorizationResult,
    CaptureResult,
    PaymentGateway,
    RefundResult,
    VoidResult,
)
from apps.payment.gateways.registry import (
    get_payment_gateway,
    register_payment_gateway,
    registered_gateways,
    unregister_payment_gateway,
)

__all__ = [
    # Base
    "PaymentGateway",
    # Results
    "AuthorizationResult",
    "CaptureResult",
    "VoidResult",
    "RefundResult",
    # Registry
    "register_payment_gateway",
    "get_payment_gateway",
    "unregister_payment_gateway",
    "registered_gateways",
]
