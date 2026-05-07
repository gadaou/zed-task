"""Payment Celery tasks.

Implements PROJECT_SPEC §4.6 — Celery-driven async payment authorisation on
the ``payments`` queue.

``authorize_payment`` is the task dispatched by ``CheckoutService`` via
``transaction.on_commit`` after a successful checkout commit.  The task is a
thin infrastructure wrapper that:

1. Fetches the ``Payment`` row and its parent tenant (cross-tenant fetch via
   ``all_tenants()`` — the task is internal and runs in a trusted worker).
2. Activates the tenant context so every ORM query inside is tenant-scoped.
3. Delegates to ``PaymentService.authorize_payment`` — all business logic and
   FSM transitions live there, not in the task.
4. Returns a summary dict for Celery result storage.

Idempotency: ``PaymentService.authorize_payment`` has an internal terminal-state
guard — if the payment is already past ``REQUIRES_CONFIRMATION``, it returns
immediately.  The task's own check at step 1 is a cheap short-circuit before
even activating tenant context.

Retry strategy: ``autoretry_for=(GatewayTimeout, GatewayUnavailable)`` with
exponential back-off + jitter, max 5 attempts.  ``GatewayDeclined`` is NOT
retried — it is a terminal gateway decision; ``PaymentService`` has already
persisted ``FAILED`` before raising it.

ADR-NOTE — ``_TERMINAL_STATUSES`` corrected:
    The previous version incorrectly included ``AUTHORIZED`` in the terminal
    set, which would have caused the task to skip capture for authorized-but-
    not-yet-captured payments.  The correct terminal set for an authorize task
    is everything that is not ``REQUIRES_CONFIRMATION``:
    ``AUTHORIZED, CAPTURED, SUCCEEDED, FAILED, CANCELLED, REFUNDED``.
    ``AUTHORIZED`` is a terminal state *for the authorize task* (it has
    already been done), but we keep the guard as "status != REQUIRES_CONFIRMATION"
    to avoid confusion.
"""

from __future__ import annotations

import logging
from uuid import UUID

from celery import shared_task

from apps.payment.exceptions import (
    GatewayDeclined,
    GatewayTimeout,
    GatewayUnavailable,
    UnsupportedGateway,
)

logger = logging.getLogger(__name__)

# The authorize task is a no-op if the payment has already moved past
# REQUIRES_CONFIRMATION — covers Celery at-least-once redelivery.
# Note: AUTHORIZED is listed here because it means the task already succeeded;
# there is no point calling authorize again.
_TERMINAL_STATUSES = frozenset(
    ["AUTHORIZED", "CAPTURED", "SUCCEEDED", "FAILED", "CANCELLED", "REFUNDED"]
)


@shared_task(
    bind=True,
    queue="payments",
    max_retries=5,
    autoretry_for=(GatewayTimeout, GatewayUnavailable),
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
)
def authorize_payment(self, payment_id: str) -> dict:
    """Authorise a payment via the pluggable gateway registry.

    Args:
        payment_id: UUID string of the ``Payment`` row to authorise.

    Returns:
        A dict with ``payment_id`` and ``status`` for Celery result storage.

    Side effects:
        Delegates all state mutations to ``PaymentService.authorize_payment``.
    """
    from apps.payment.models import Payment
    from apps.payment.services import PaymentService
    from apps.tenant.context import tenant_context

    payment_uuid = UUID(payment_id)

    # ------------------------------------------------------------------
    # Fast idempotency check — no tenant context needed for a status read.
    # ------------------------------------------------------------------
    try:
        payment = (
            Payment.objects.all_tenants()
            .select_related("tenant")
            .get(pk=payment_uuid)
        )
    except Payment.DoesNotExist:
        logger.error(
            "authorize_payment: Payment %s not found — skipping.",
            payment_id,
        )
        return {"payment_id": payment_id, "status": "not_found"}

    if payment.status in _TERMINAL_STATUSES:
        logger.info(
            "authorize_payment: Payment %s already in terminal state %s — idempotent skip.",
            payment_id,
            payment.status,
        )
        return {"payment_id": payment_id, "status": payment.status}

    # ------------------------------------------------------------------
    # Delegate to the service layer.
    # ------------------------------------------------------------------
    with tenant_context(payment.tenant):
        try:
            updated = PaymentService().authorize_payment(payment_uuid)
            logger.info(
                "authorize_payment: Payment %s → %s",
                payment_id,
                updated.status,
            )
            return {"payment_id": payment_id, "status": updated.status}

        except GatewayDeclined as exc:
            # Service has already written FAILED to the DB — just log and return.
            logger.info(
                "authorize_payment: Payment %s declined by gateway: %s",
                payment_id,
                exc.error_code,
            )
            return {"payment_id": payment_id, "status": "FAILED"}

        except UnsupportedGateway as exc:
            # Configuration error — no point retrying.
            logger.error(
                "authorize_payment: Payment %s uses unregistered gateway '%s' — "
                "marking FAILED (no retry).",
                payment_id,
                exc.slug,
            )
            from django.db import transaction

            with transaction.atomic():
                Payment.objects.filter(
                    pk=payment_uuid,
                    status=Payment.Status.REQUIRES_CONFIRMATION,
                ).update(
                    status=Payment.Status.FAILED,
                    failure_reason=str(exc)[:255],
                )
            return {"payment_id": payment_id, "status": "FAILED"}

        # GatewayTimeout and GatewayUnavailable are handled by autoretry_for
        # and propagate naturally — no explicit except needed.


def enqueue_authorize_payment(payment_id: UUID) -> None:
    """Dispatch ``authorize_payment`` to the Celery ``payments`` queue.

    This is the function referenced by ``CheckoutService._payment_dispatcher``.
    Wrapping the task dispatch in a named function keeps the ``CheckoutService``
    independent of Celery imports — tests can replace this with a no-op lambda.

    Must be called inside ``transaction.on_commit(...)`` so the task is only
    enqueued after a successful commit.  A rolled-back checkout must never
    produce a payment task.
    """
    authorize_payment.apply_async(
        args=[str(payment_id)],
        queue="payments",
    )


# ---------------------------------------------------------------------------
# process_payment — order-keyed entrypoint
# ---------------------------------------------------------------------------


@shared_task(
    bind=True,
    queue="payments",
    max_retries=5,
    autoretry_for=(GatewayTimeout, GatewayUnavailable),
    retry_backoff=True,
    retry_jitter=True,
    acks_late=True,
)
def process_payment(self, order_id: str) -> dict:
    """Process (authorise) the payment for the given order.

    This is a thin order-keyed wrapper around the ``authorize_payment``
    task and ``PaymentService.authorize_payment``.  It resolves the
    ``Payment`` from the ``Order``, performs a cheap idempotency check,
    and delegates all FSM transitions to the service layer.

    Args:
        order_id: UUID string of the ``Order`` whose payment should be
                  authorised.

    Returns:
        A dict with ``order_id``, ``payment_id``, and ``status`` for
        Celery result storage.

    Retry strategy:
        ``GatewayTimeout`` and ``GatewayUnavailable`` are retried with
        exponential back-off + jitter, max 5 attempts.
        ``GatewayDeclined`` is terminal — the service has already written
        ``FAILED`` to the DB; no retry.

    Idempotency:
        If the resolved payment is already in a terminal status, the task
        returns immediately without calling the gateway.  The service layer
        adds a second guard (terminal-state check) and the DB enforces
        atomicity via a status-guarded ``UPDATE … WHERE status=REQUIRES_CONFIRMATION``.
    """
    from apps.order.models import Order
    from apps.payment.models import Payment
    from apps.payment.services import PaymentService
    from apps.tenant.context import tenant_context

    order_uuid = UUID(order_id)

    # ------------------------------------------------------------------
    # Load the order cross-tenant to resolve the tenant context.
    # ------------------------------------------------------------------
    try:
        order = (
            Order.objects.all_tenants()
            .select_related("tenant")
            .get(pk=order_uuid)
        )
    except Order.DoesNotExist:
        logger.error(
            "process_payment: Order %s not found — skipping.",
            order_id,
        )
        return {"order_id": order_id, "status": "not_found"}

    with tenant_context(order.tenant):
        # ------------------------------------------------------------------
        # Resolve the Payment linked to the order's cart.
        # ------------------------------------------------------------------
        payment = (
            Payment.objects.filter(cart=order.cart)
            .order_by("-created_at")
            .first()
        )

        if payment is None:
            logger.error(
                "process_payment: No Payment found for order %s (cart %s) — skipping.",
                order_id,
                order.cart_id,
            )
            return {"order_id": order_id, "status": "no_payment"}

        # ------------------------------------------------------------------
        # Fast idempotency check — skip if already past REQUIRES_CONFIRMATION.
        # ------------------------------------------------------------------
        if payment.status in _TERMINAL_STATUSES:
            logger.info(
                "process_payment: Payment %s for order %s already in terminal state %s — idempotent skip.",
                payment.id,
                order_id,
                payment.status,
            )
            return {
                "order_id": order_id,
                "payment_id": str(payment.id),
                "status": payment.status,
            }

        logger.info(
            "process_payment: authorising payment %s for order %s via provider '%s'",
            payment.id,
            order_id,
            payment.provider,
        )

        # ------------------------------------------------------------------
        # Delegate to the service layer.
        # ------------------------------------------------------------------
        try:
            updated = PaymentService().authorize_payment(payment.id)
            logger.info(
                "process_payment: Payment %s for order %s → %s",
                payment.id,
                order_id,
                updated.status,
            )
            return {
                "order_id": order_id,
                "payment_id": str(payment.id),
                "status": updated.status,
            }

        except GatewayDeclined as exc:
            # Service has already written FAILED to the DB.
            logger.info(
                "process_payment: Payment %s for order %s declined by gateway: %s",
                payment.id,
                order_id,
                exc.error_code,
            )
            return {
                "order_id": order_id,
                "payment_id": str(payment.id),
                "status": "FAILED",
            }

        except UnsupportedGateway as exc:
            # Configuration error — no point retrying.
            logger.error(
                "process_payment: Payment %s for order %s uses unregistered gateway '%s' — "
                "marking FAILED (no retry).",
                payment.id,
                order_id,
                exc.slug,
            )
            from django.db import transaction as _tx

            with _tx.atomic():
                Payment.objects.filter(
                    pk=payment.pk,
                    status=Payment.Status.REQUIRES_CONFIRMATION,
                ).update(
                    status=Payment.Status.FAILED,
                    failure_reason=str(exc)[:255],
                )
            return {
                "order_id": order_id,
                "payment_id": str(payment.id),
                "status": "FAILED",
            }

        # GatewayTimeout and GatewayUnavailable are handled by autoretry_for
        # and propagate naturally — no explicit except needed.


def enqueue_process_payment(order_id: UUID) -> None:
    """Dispatch ``process_payment`` to the Celery ``payments`` queue.

    This is an **alternative, order-keyed** entry-point for payment
    authorisation.  It is NOT wired into the current checkout path —
    ``CheckoutService`` dispatches ``enqueue_authorize_payment`` (the
    payment-keyed entry-point) instead.  ``enqueue_process_payment`` is
    available for ops tooling or future flows that have an ``order_id``
    but not a ``payment_id`` at dispatch time.

    Wrapping the task dispatch in a named function keeps callers
    independent of Celery imports — tests can replace this with a no-op.

    Must be called inside ``transaction.on_commit(...)`` so the task is
    only enqueued after a successful commit.
    """
    process_payment.apply_async(
        args=[str(order_id)],
        queue="payments",
    )
