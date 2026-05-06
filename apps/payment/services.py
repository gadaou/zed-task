"""Payment services — ``PaymentService``.

Implements PROJECT_SPEC §3.3 (pluggable gateway interface + registry), §4.6
(Celery-driven async authorisation), and §5.3 (``Payment`` FSM transitions).

Public API
----------
``PaymentService`` class with four public methods:

    ``authorize_payment(payment_id) -> Payment``
    ``capture_payment(payment_id) -> Payment``
    ``void_payment(payment_id) -> Payment``
    ``refund_payment(payment_id, amount=None) -> Payment``

All four follow the same shape:
    1. Load the ``Payment`` row.
    2. Terminal-state guard (idempotency — if already in the target/terminal
       state, return immediately without a second gateway call).
    3. Look up the gateway via ``get_payment_gateway(payment.provider)`` — no
       ``if/else`` on slug anywhere in this module.
    4. Call the appropriate gateway method.
    5. Inside ``transaction.atomic()`` perform a **status-guarded UPDATE**
       (``WHERE pk=? AND status=<expected_source_state>``) so that a concurrent
       retry racing the same row cannot double-transition.
    6. Log and return the freshly-saved instance.

Idempotency guarantee: the status-guarded UPDATE is the enforcement mechanism.
If a second call arrives after the first has already transitioned the row, the
``WHERE`` clause matches zero rows and the method returns the current state
without calling the gateway again.

ADR-NOTE — why the service, not the task, calls the gateway:
    PROJECT_SPEC §4.1 mandates that business logic lives in the service layer.
    The Celery task (``apps.payment.tasks``) is a thin infrastructure wrapper;
    it resolves tenant context, delegates to ``PaymentService``, and handles
    Celery-specific concerns (retries, acks_late).  The service has no Celery
    import.

ADR-NOTE — ``Order`` lookup inside ``authorize_payment``:
    The ``Payment`` row carries a ``cart`` FK; the ``Order`` is fetched via
    ``Order.objects.filter(cart=payment.cart).first()``.  If no order exists
    (payment was created before the order, which should not happen in the
    current flow), a ``ValueError`` is raised rather than silently proceeding
    with ``order=None`` — fail loudly in development (PROJECT_SPEC §7.7).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.db.models import F

from apps.invoice.tasks import enqueue_generate_invoice
from apps.payment.exceptions import (
    GatewayDeclined,
    GatewayTimeout,
    GatewayUnavailable,
)
from apps.payment.gateways import get_payment_gateway

logger = logging.getLogger(__name__)

# These statuses are already past authorization — no second gateway call needed.
_ALREADY_AUTHORIZED = frozenset(
    ["AUTHORIZED", "CAPTURED", "SUCCEEDED", "REFUNDED"]
)

# These statuses indicate a terminal failure — also idempotent-skip.
_TERMINAL_FAILURE = frozenset(["FAILED", "CANCELLED"])


class PaymentService:
    """Service layer for all payment state transitions.

    Stateless — safe to instantiate per-call or as a singleton.  All
    collaborators (models, gateway registry) are resolved at call time via
    normal imports so the class can be instantiated without a DB connection
    (useful for contract tests that only exercise the gateway layer).
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def authorize_payment(self, payment_id: UUID) -> "Payment":  # type: ignore[name-defined]
        """Authorise the charge for ``payment_id``.

        Transitions ``Payment.status``:
            ``REQUIRES_CONFIRMATION``  →  ``AUTHORIZED``  (success)
            ``REQUIRES_CONFIRMATION``  →  ``FAILED``       (gateway declined)

        On ``GatewayTimeout`` / ``GatewayUnavailable`` the row is left in
        ``REQUIRES_CONFIRMATION`` and the exception propagates so the Celery
        task's ``autoretry_for`` mechanism can reschedule.

        Returns the updated ``Payment`` instance (re-fetched from DB).
        """
        from apps.order.models import Order
        from apps.payment.models import Payment, PaymentMethod

        payment = self._load(payment_id)

        if payment.status in _ALREADY_AUTHORIZED | _TERMINAL_FAILURE:
            logger.info(
                "PaymentService.authorize_payment: payment %s already in %s — idempotent skip.",
                payment_id,
                payment.status,
            )
            return payment

        gateway = get_payment_gateway(payment.provider)

        # Find the Order created by CheckoutService for this cart.
        order = Order.objects.filter(cart=payment.cart).first()
        if order is None:
            raise ValueError(
                f"No Order found for cart {payment.cart_id} — "
                "authorize_payment must be called after checkout commits."
            )

        # Load the PaymentMethod that was used at checkout.
        try:
            payment_method = PaymentMethod.objects.get(pk=order.payment_method_id)
        except PaymentMethod.DoesNotExist:
            # ADR-NOTE: PaymentMethod may have been soft-deleted after checkout;
            # we proceed with payment_method=None so gateways that don't need
            # the instrument data can still proceed.  Real integrations that
            # require it should raise GatewayUnavailable from within the gateway.
            payment_method = None

        logger.info(
            "PaymentService.authorize_payment: calling gateway '%s' for payment %s amount=%s%s",
            payment.provider,
            payment_id,
            payment.amount,
            payment.currency,
        )

        # Gateway call — GatewayTimeout / GatewayUnavailable propagate to the
        # caller (Celery task) without mutating the row.
        result = gateway.authorize_payment(order, payment_method)

        if result.success:
            with transaction.atomic():
                rows = Payment.objects.filter(
                    pk=payment.pk,
                    status=Payment.Status.REQUIRES_CONFIRMATION,
                ).update(
                    status=Payment.Status.AUTHORIZED,
                    gateway_authorization_id=result.reference,
                )
                if rows == 0:
                    logger.warning(
                        "PaymentService.authorize_payment: payment %s status changed before "
                        "AUTHORIZED update — concurrent transition, idempotent skip.",
                        payment_id,
                    )

                # Flip Order.status to PAID (status-guarded — safe against
                # concurrent retries of this same task).
                Order.objects.all_tenants().filter(
                    pk=order.pk,
                    status="PENDING_PAYMENT",
                ).update(
                    status="PAID",
                    version=F("version") + 1,
                )

                # Dispatch invoice generation only on commit so a rolled-back
                # payment transition never produces an orphan invoice task.
                order_id_snapshot = order.id

                def _dispatch_invoice() -> None:
                    enqueue_generate_invoice(order_id_snapshot)

                transaction.on_commit(_dispatch_invoice)

            payment.refresh_from_db()
            logger.info("PaymentService.authorize_payment: payment %s → AUTHORIZED", payment_id)
        else:
            with transaction.atomic():
                Payment.objects.filter(
                    pk=payment.pk,
                    status=Payment.Status.REQUIRES_CONFIRMATION,
                ).update(
                    status=Payment.Status.FAILED,
                    failure_reason=(
                        f"{result.error_code}: {result.error_message}"
                    )[:255],
                )

                # Flip Order.status to FAILED (status-guarded).
                Order.objects.all_tenants().filter(
                    pk=order.pk,
                    status="PENDING_PAYMENT",
                ).update(
                    status="FAILED",
                    version=F("version") + 1,
                )

            payment.refresh_from_db()
            logger.info(
                "PaymentService.authorize_payment: payment %s → FAILED (%s)",
                payment_id,
                result.error_code,
            )
            raise GatewayDeclined(
                error_code=result.error_code,
                detail=result.error_message,
            )

        return payment

    def capture_payment(self, payment_id: UUID) -> "Payment":  # type: ignore[name-defined]
        """Capture a previously authorised payment.

        Transitions: ``AUTHORIZED`` → ``CAPTURED``.
        Idempotent: if already ``CAPTURED`` / ``SUCCEEDED``, returns current state.
        """
        from apps.payment.models import Payment

        payment = self._load(payment_id)

        if payment.status in ("CAPTURED", "SUCCEEDED", "REFUNDED"):
            logger.info(
                "PaymentService.capture_payment: payment %s already in %s — idempotent skip.",
                payment_id,
                payment.status,
            )
            return payment

        if payment.status != Payment.Status.AUTHORIZED:
            raise ValueError(
                f"Cannot capture payment {payment_id} in status '{payment.status}'. "
                "Expected AUTHORIZED."
            )

        gateway = get_payment_gateway(payment.provider)
        result = gateway.capture_payment(payment.gateway_authorization_id)

        if result.success:
            with transaction.atomic():
                rows = Payment.objects.filter(
                    pk=payment.pk,
                    status=Payment.Status.AUTHORIZED,
                ).update(
                    status=Payment.Status.CAPTURED,
                    gateway_capture_id=result.reference,
                )
                if rows == 0:
                    logger.warning(
                        "PaymentService.capture_payment: payment %s status changed before "
                        "CAPTURED update — concurrent transition.",
                        payment_id,
                    )
            payment.refresh_from_db()
            logger.info("PaymentService.capture_payment: payment %s → CAPTURED", payment_id)
        else:
            with transaction.atomic():
                Payment.objects.filter(
                    pk=payment.pk,
                    status=Payment.Status.AUTHORIZED,
                ).update(
                    status=Payment.Status.FAILED,
                    failure_reason=(
                        f"{result.error_code}: {result.error_message}"
                    )[:255],
                )
            payment.refresh_from_db()
            logger.info(
                "PaymentService.capture_payment: payment %s → FAILED (%s)",
                payment_id,
                result.error_code,
            )
            raise GatewayDeclined(error_code=result.error_code, detail=result.error_message)

        return payment

    def void_payment(self, payment_id: UUID) -> "Payment":  # type: ignore[name-defined]
        """Void (cancel) a previously authorised payment before capture.

        Transitions: ``AUTHORIZED`` → ``CANCELLED``.
        Idempotent: if already ``CANCELLED``, returns current state.
        """
        from apps.payment.models import Payment

        payment = self._load(payment_id)

        if payment.status == Payment.Status.CANCELLED:
            logger.info(
                "PaymentService.void_payment: payment %s already CANCELLED — idempotent skip.",
                payment_id,
            )
            return payment

        if payment.status != Payment.Status.AUTHORIZED:
            raise ValueError(
                f"Cannot void payment {payment_id} in status '{payment.status}'. "
                "Expected AUTHORIZED."
            )

        gateway = get_payment_gateway(payment.provider)
        result = gateway.void_payment(payment.gateway_authorization_id)

        if result.success:
            with transaction.atomic():
                rows = Payment.objects.filter(
                    pk=payment.pk,
                    status=Payment.Status.AUTHORIZED,
                ).update(status=Payment.Status.CANCELLED)
                if rows == 0:
                    logger.warning(
                        "PaymentService.void_payment: payment %s concurrent transition.",
                        payment_id,
                    )
            payment.refresh_from_db()
            logger.info("PaymentService.void_payment: payment %s → CANCELLED", payment_id)
        else:
            raise GatewayDeclined(error_code=result.error_code, detail=result.error_message)

        return payment

    def refund_payment(
        self,
        payment_id: UUID,
        amount: Decimal | None = None,
    ) -> "Payment":  # type: ignore[name-defined]
        """Issue a full or partial refund for a captured/succeeded payment.

        Transitions: ``CAPTURED`` | ``SUCCEEDED`` → ``REFUNDED``.
        Idempotent: if already ``REFUNDED``, returns current state.
        """
        from apps.payment.models import Payment

        payment = self._load(payment_id)

        if payment.status == Payment.Status.REFUNDED:
            logger.info(
                "PaymentService.refund_payment: payment %s already REFUNDED — idempotent skip.",
                payment_id,
            )
            return payment

        if payment.status not in (Payment.Status.CAPTURED, Payment.Status.SUCCEEDED):
            raise ValueError(
                f"Cannot refund payment {payment_id} in status '{payment.status}'. "
                "Expected CAPTURED or SUCCEEDED."
            )

        gateway = get_payment_gateway(payment.provider)
        result = gateway.refund_payment(payment.gateway_capture_id, amount=amount)

        if result.success:
            with transaction.atomic():
                rows = Payment.objects.filter(
                    pk=payment.pk,
                    status__in=[Payment.Status.CAPTURED, Payment.Status.SUCCEEDED],
                ).update(status=Payment.Status.REFUNDED)
                if rows == 0:
                    logger.warning(
                        "PaymentService.refund_payment: payment %s concurrent transition.",
                        payment_id,
                    )
            payment.refresh_from_db()
            logger.info("PaymentService.refund_payment: payment %s → REFUNDED", payment_id)
        else:
            raise GatewayDeclined(error_code=result.error_code, detail=result.error_message)

        return payment

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load(payment_id: UUID) -> "Payment":  # type: ignore[name-defined]
        """Load a ``Payment`` row, bypassing the tenant-scoped manager.

        This method is called from within a ``tenant_context`` (set by the
        Celery task or the view), so a plain ``all_tenants()`` fetch is used
        here to avoid depending on the calling code having set the context var
        before the service is instantiated.  The Celery task always sets
        ``tenant_context`` before delegating here.
        """
        from apps.payment.models import Payment

        try:
            return Payment.objects.all_tenants().select_related("cart", "tenant").get(pk=payment_id)
        except Payment.DoesNotExist:
            raise ValueError(f"Payment {payment_id} not found.")
