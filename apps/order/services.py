"""Order services — ``CheckoutService``.

Implements PROJECT_SPEC §2 op 7 (checkout) and the full flow specified in
``docs/architecture.md §8``.

Public API
----------
``CheckoutService`` class with one public method:

    ``checkout(*, cart_id, payment_method_id, address_id, idempotency_key,
               request_hash) -> CheckoutResult``

Algorithm (each step is a distinct section below):

    1. Idempotency check — Redis fast path + Postgres durable replay.
    2. Mark in-progress in Redis (NX).
    3. Acquire distributed lock ``lock:checkout:{tenant_id}:{cart_id}``.
    4. Open ``transaction.atomic()``.
    5. Lock cart row ``SELECT FOR UPDATE``; validate ACTIVE status.
    6. Lock address + payment_method rows ``SELECT FOR UPDATE``.
    7. Empty-cart guard.
    8. Revalidate coupons (existing ``CouponService.revalidate_cart_coupons``).
    9. Conditional stock decrement ``UPDATE … WHERE stock >= qty`` (per item).
   10. Create ``Order`` + ``OrderItem`` rows.
   11. Mark cart ``CHECKED_OUT`` with optimistic-version guard.
   12. Create ``Payment`` in ``REQUIRES_CONFIRMATION``.
   13. Persist ``IdempotencyRecord(status=SUCCEEDED, response=...)``.
   14. Register ``transaction.on_commit`` hook to dispatch the Celery task.
   15. COMMIT (on_commit hook fires synchronously in tests via ALWAYS_EAGER).
   16. Release lock + clear in-progress sentinel in ``try/finally``.
   17. Return ``CheckoutResult``.

Failure handling:
    Any domain exception inside ``transaction.atomic()`` rolls back all writes
    atomically — stock is restored, order is gone, idempotency record absent.
    The lock and Redis sentinel are always cleared in the outer ``finally``
    block so the client may retry.

Concurrency model:
    - Redis lock serialises concurrent checkouts on the same cart to one at a
      time (coordination optimisation — PROJECT_SPEC §4.4).
    - ``SELECT FOR UPDATE`` on the cart row is the actual DB-level safety net
      (PROJECT_SPEC §3.4).
    - Idempotency deduplicates retries after the lock is released.

Injected collaborators (all overridable in tests):
    - ``redis_client``        — ``redis.Redis`` (default: process singleton).
    - ``coupon_service``      — ``CouponService`` (default: fresh instance).
    - ``payment_dispatcher``  — ``Callable[[UUID], None]`` (default: Celery task).
    - ``idem_manager``        — ``IdempotencyManager`` (default: fresh instance).
    - ``lock_ttl_ms``         — int (default: ``settings.CHECKOUT_LOCK_TTL_MS``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

import redis
from django.conf import settings
from django.db import transaction
from django.db.models import F

from apps.cart.models import Cart, CartItem
from apps.catalog.models import Product
from apps.core.cache import schedule_cart_cache_invalidation
from apps.core.idempotency import AcquireOutcome, IdempotencyManager
from apps.core.locks import redis_lock
from apps.core.logging import log_event
from apps.core.metrics import incr
from apps.core.models import IdempotencyRecord
from apps.coupon.services import CouponService
from apps.order.exceptions import (
    AddressNotFound,
    CartAlreadyCheckedOut,
    CartEmpty,
    CartNotFound,
    CartStaleVersion,
    PaymentMethodInvalid,
    ProductOutOfStock,
)
from apps.order.models import Order, OrderItem
from apps.payment.models import Payment, PaymentMethod
from apps.payment.tasks import enqueue_authorize_payment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckoutResult:
    """Value object returned by ``CheckoutService.checkout``.

    ``http_status`` is 202 for async gateways (pending authorisation) or 200
    for inline gateways that resolve synchronously.  The mock gateway always
    returns 202 — the final state arrives via the Celery task.
    """

    order_id: UUID
    payment_id: UUID
    payment_status: str
    total: str
    currency: str
    http_status: int = 202


class CheckoutService:
    """Service for the checkout linearization point.

    Designed as a class (not a module-level function) because it carries
    injectable collaborators — the same pattern used by ``CouponService``.
    Tests can replace any collaborator via ``__init__`` without monkeypatching
    module-level names.
    """

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        coupon_service: CouponService | None = None,
        payment_dispatcher: Callable[[UUID], None] | None = None,
        idem_manager: IdempotencyManager | None = None,
        lock_ttl_ms: int | None = None,
    ) -> None:
        self._redis = redis_client
        self._coupon_service = coupon_service or CouponService()
        self._payment_dispatcher: Callable[[UUID], None] = (
            payment_dispatcher or enqueue_authorize_payment
        )
        self._idem_manager = idem_manager
        self._lock_ttl_ms = lock_ttl_ms if lock_ttl_ms is not None else settings.CHECKOUT_LOCK_TTL_MS

    @property
    def _redis_client(self) -> redis.Redis:
        if self._redis is None:
            from apps.core.redis import get_redis_client
            self._redis = get_redis_client()
        return self._redis

    @property
    def _idempotency_manager(self) -> IdempotencyManager:
        if self._idem_manager is None:
            self._idem_manager = IdempotencyManager(
                redis_client=self._redis_client,
                inprogress_ttl_s=settings.IDEMPOTENCY_INPROGRESS_TTL_S,
            )
        return self._idem_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def checkout(
        self,
        *,
        cart_id: UUID,
        payment_method_id: UUID,
        address_id: UUID,
        idempotency_key: UUID,
        request_hash: str,
    ) -> CheckoutResult:
        """Execute the full checkout flow for ``cart_id``.

        See module docstring for the complete algorithm.

        Args:
            cart_id:            UUID of the cart to check out.
            payment_method_id:  UUID of the PaymentMethod to charge.
            address_id:         UUID of the shipping Address.
            idempotency_key:    Client-supplied ``Idempotency-Key`` UUID.
            request_hash:       ``compute_request_hash`` digest of the request
                                payload, used to detect conflict replays.

        Returns:
            ``CheckoutResult`` with order/payment identifiers and status.

        Raises:
            CartNotFound:             cart_id not found in this tenant.
            CartAlreadyCheckedOut:    cart is not in ACTIVE status.
            CartEmpty:                cart has no items.
            CartStaleVersion:         concurrent modification detected.
            ProductOutOfStock:        a cart item cannot be fulfilled.
            AddressNotFound:          address_id not found for this user.
            PaymentMethodInvalid:     payment_method_id not found.
            CouponDomainError:        a coupon revalidation failed.
            LockNotAcquired:          concurrent checkout holds the lock.
            IdempotencyConflict:      same key + different request payload.
            IdempotencyInProgress:    same key currently being processed.
        """
        from apps.tenant.context import get_current_tenant

        tenant = get_current_tenant()
        tenant_id: UUID = tenant.id

        t0 = time.monotonic()
        log_event(
            logger,
            "checkout.started",
            cart_id=str(cart_id),
            tenant_id=str(tenant_id),
        )

        # ------------------------------------------------------------------
        # Step 1 — Idempotency check (outside lock, outside DB transaction).
        # ------------------------------------------------------------------
        acquire_result = self._idempotency_manager.check_for_replay(
            tenant_id=tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if acquire_result.outcome == AcquireOutcome.REPLAY:
            record = acquire_result.record
            log_event(
                logger,
                "checkout.completed",
                outcome="replay",
                cart_id=str(cart_id),
                tenant_id=str(tenant_id),
                duration_ms=round((time.monotonic() - t0) * 1000),
            )
            return self._result_from_record(record)

        # ------------------------------------------------------------------
        # Step 2 — Mark in-progress in Redis (NX).
        # Raises IdempotencyInProgress if a concurrent request beat us here.
        # ------------------------------------------------------------------
        self._idempotency_manager.mark_in_progress(tenant_id, idempotency_key)

        # The in-progress sentinel is always cleared in the outer finally
        # so the client can retry after a transient failure.
        lock_key = f"lock:checkout:{tenant_id}:{cart_id}"
        try:
            # --------------------------------------------------------------
            # Step 3 — Acquire distributed lock.
            # LockNotAcquired surfaces as cart/locked to the client.
            # --------------------------------------------------------------
            from apps.core.exceptions import LockNotAcquired as _LockNotAcquired

            try:
                with redis_lock(lock_key, self._lock_ttl_ms, client=self._redis_client):
                    result = self._run_checkout_transaction(
                        cart_id=cart_id,
                        payment_method_id=payment_method_id,
                        address_id=address_id,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        tenant_id=tenant_id,
                    )
            except _LockNotAcquired:
                log_event(
                    logger,
                    "checkout.lock_failed",
                    level=logging.WARNING,
                    cart_id=str(cart_id),
                    tenant_id=str(tenant_id),
                    lock_key=lock_key,
                    duration_ms=round((time.monotonic() - t0) * 1000),
                )
                incr("checkout.lock_contention", tenant_id=str(tenant_id))
                raise
            except Exception as exc:
                log_event(
                    logger,
                    "checkout.failed",
                    level=logging.ERROR,
                    cart_id=str(cart_id),
                    tenant_id=str(tenant_id),
                    reason=type(exc).__name__,
                    duration_ms=round((time.monotonic() - t0) * 1000),
                )
                incr("checkout.failed", reason=type(exc).__name__, tenant_id=str(tenant_id))
                raise
        finally:
            # Always clear the in-progress sentinel so the client can retry
            # on transient failures.  Done after the lock is released so a
            # late retry can't sneak in before the transaction is committed.
            self._idempotency_manager.clear_in_progress(tenant_id, idempotency_key)

        log_event(
            logger,
            "checkout.completed",
            outcome="success",
            cart_id=str(cart_id),
            tenant_id=str(tenant_id),
            order_id=str(result.order_id),
            payment_id=str(result.payment_id),
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        return result

    # ------------------------------------------------------------------
    # Internal — DB transaction
    # ------------------------------------------------------------------

    @transaction.atomic
    def _run_checkout_transaction(
        self,
        *,
        cart_id: UUID,
        payment_method_id: UUID,
        address_id: UUID,
        idempotency_key: UUID,
        request_hash: str,
        tenant_id: UUID,
    ) -> CheckoutResult:
        """Execute the Postgres transaction for the checkout.

        Must only be called with the distributed lock already held.
        The ``@transaction.atomic`` decorator opens the transaction boundary;
        any exception raised inside causes a rollback.
        """

        # ------------------------------------------------------------------
        # Step 4 — Lock the cart row.
        # ------------------------------------------------------------------
        try:
            cart = Cart.objects.select_for_update().get(pk=cart_id)
        except Cart.DoesNotExist:
            raise CartNotFound(f"cart {cart_id} not found")

        if cart.status != Cart.Status.ACTIVE:
            raise CartAlreadyCheckedOut(
                f"cart {cart_id} is already checked out (status={cart.status})"
            )

        # ------------------------------------------------------------------
        # Step 5 — Lock address + payment_method.
        # ------------------------------------------------------------------
        from apps.addresses.models import Address

        try:
            address = Address.objects.select_for_update().get(
                pk=address_id,
                user_id=cart.user_id,
                deleted_at__isnull=True,
            )
        except Address.DoesNotExist:
            raise AddressNotFound(
                f"address {address_id} not found for user {cart.user_id}"
            )

        try:
            payment_method = PaymentMethod.objects.select_for_update().get(
                pk=payment_method_id
            )
        except PaymentMethod.DoesNotExist:
            raise PaymentMethodInvalid(
                f"payment method {payment_method_id} not found in this tenant"
            )

        # ------------------------------------------------------------------
        # Step 6 — Empty-cart guard.
        # ------------------------------------------------------------------
        items = list(
            CartItem.objects.filter(cart=cart).select_related()
        )
        if not items:
            raise CartEmpty(f"cart {cart_id} has no items")

        # ------------------------------------------------------------------
        # Step 7 — Revalidate coupons.
        # Raises CouponDomainError if any applied coupon is now invalid.
        # ------------------------------------------------------------------
        self._coupon_service.revalidate_cart_coupons(cart)

        # Refresh cart totals after coupon revalidation (revalidate may bump version).
        cart.refresh_from_db(
            fields=["total_price", "discount_amount", "total_after_discount", "version"]
        )

        # ------------------------------------------------------------------
        # Step 8 — Conditional stock decrement (one UPDATE per item).
        # The WHERE stock >= qty guard closes the race window that a plain
        # F-expression decrement cannot.  Zero rows updated → out of stock.
        # This is the same pattern as Coupon._atomic_increment_used_count.
        # ------------------------------------------------------------------
        for item in items:
            rows = Product.objects.filter(
                pk=item.product_id,
                stock__gte=item.quantity,
            ).update(stock=F("stock") - item.quantity)
            if rows == 0:
                raise ProductOutOfStock(product_id=item.product_id)

        # ------------------------------------------------------------------
        # Step 9 — Create Order + OrderItems.
        # ------------------------------------------------------------------
        order = Order.objects.create(
            user_id=cart.user_id,
            cart=cart,
            address=address,
            payment_method=payment_method,
            subtotal=cart.total_price,
            discount_amount=cart.discount_amount,
            total=cart.total_after_discount,
            currency=cart.currency,
            idempotency_key=idempotency_key,
        )

        OrderItem.objects.bulk_create([
            OrderItem(
                tenant=order.tenant,
                order=order,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.price_snapshot,
                currency=item.currency,
            )
            for item in items
        ])

        # ------------------------------------------------------------------
        # Step 10 — Mark cart CHECKED_OUT with optimistic-version guard.
        # If a concurrent write changed the cart between our SELECT FOR UPDATE
        # and this UPDATE (impossible with a row lock, but guarded for
        # defence-in-depth), we raise CartStaleVersion.
        # ------------------------------------------------------------------
        rows = Cart.objects.filter(
            pk=cart.pk,
            version=cart.version,
            status=Cart.Status.ACTIVE,
        ).update(
            status=Cart.Status.CHECKED_OUT,
            version=F("version") + 1,
        )
        if rows == 0:
            raise CartStaleVersion(
                f"cart {cart_id} version={cart.version} changed mid-checkout"
            )

        # Schedule cache invalidation after commit so:
        # - A successful COMMIT purges the stale ACTIVE cart cache entry;
        #   the next GET /cart transparently creates/reuses a fresh ACTIVE cart.
        # - A ROLLBACK leaves the on_commit hook un-invoked, keeping the
        #   cached ACTIVE cart intact (the mutation never happened).
        schedule_cart_cache_invalidation(tenant_id, cart.user_id)

        # ------------------------------------------------------------------
        # Step 11 — Create Payment in REQUIRES_CONFIRMATION.
        # ------------------------------------------------------------------
        payment = Payment.objects.create(
            cart=cart,
            provider=payment_method.gateway_slug,
            amount=cart.total_after_discount,
            currency=cart.currency,
        )

        # ------------------------------------------------------------------
        # Step 12 — Persist IdempotencyRecord inside this transaction so that
        # a rollback also removes the record.
        # ------------------------------------------------------------------
        response_payload = {
            "order_id": str(order.id),
            "payment_id": str(payment.id),
            "payment_status": "pending",
            "total": str(order.total),
            "currency": order.currency,
        }
        IdempotencyRecord.objects.create(
            tenant_id=tenant_id,
            key=idempotency_key,
            request_hash=request_hash,
            status=IdempotencyRecord.Status.SUCCEEDED,
            response_status=202,
            response_body=response_payload,
        )

        # ------------------------------------------------------------------
        # Step 13 — Dispatch the Celery task on commit.
        # on_commit fires ONLY on a successful COMMIT.  A rolled-back
        # transaction leaves the lambda un-invoked — no orphan payment tasks.
        # ------------------------------------------------------------------
        payment_id_snapshot = payment.id

        def _dispatch() -> None:
            self._payment_dispatcher(payment_id_snapshot)

        transaction.on_commit(_dispatch)

        log_event(
            logger,
            "checkout.transaction_committed",
            order_id=str(order.id),
            payment_id=str(payment.id),
            cart_id=str(cart_id),
            tenant_id=str(tenant_id),
        )

        return CheckoutResult(
            order_id=order.id,
            payment_id=payment.id,
            payment_status="pending",
            total=str(order.total),
            currency=order.currency,
            http_status=202,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_from_record(record: IdempotencyRecord) -> CheckoutResult:
        """Reconstruct a ``CheckoutResult`` from a stored ``IdempotencyRecord``."""
        body = record.response_body or {}
        return CheckoutResult(
            order_id=UUID(body["order_id"]),
            payment_id=UUID(body["payment_id"]),
            payment_status=body.get("payment_status", "pending"),
            total=body.get("total", "0.00"),
            currency=body.get("currency", ""),
            http_status=record.response_status or 202,
        )
