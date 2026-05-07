"""Idempotency primitives for the checkout endpoint.

Implements the two-phase idempotency strategy from PROJECT_SPEC §4.5:

Phase 1 — Redis in-progress sentinel (before the DB transaction):
    ``SET idem:{tenant_id}:{key} "in_progress" NX PX {ttl_seconds * 1000}``
    A retry that arrives while this key is set gets ``409 in-progress``.
    The sentinel is cleared in a ``finally`` block after the transaction
    (success or failure) so the client may retry on transient failures.

Phase 2 — Durable Postgres record (inside the DB transaction):
    An ``IdempotencyRecord`` row is INSERTed with ``status=SUCCEEDED`` and
    the serialised response *inside the same checkout transaction*.  If the
    transaction rolls back, the record disappears with it — no stale
    "succeeded" records for orders that were never created.

Replay logic (checked before acquiring the lock):
    1. Redis has no in-progress sentinel → proceed to phase 1.
    2. Postgres has a record for (tenant, key):
       a. Same ``request_hash`` → return the stored response (replay).
       b. Different ``request_hash`` → raise ``IdempotencyConflict``.
    3. Redis in-progress sentinel exists → raise ``IdempotencyInProgress``.
    4. Neither → FRESH, proceed to acquire the lock and run checkout.

Public API
----------
- ``compute_request_hash(payload: dict) -> str``
    SHA-256 of the deterministic JSON serialisation of ``payload``.

- ``IdempotencyManager`` class with:
    - ``check_for_replay(tenant_id, key, request_hash) -> AcquireResult``
    - ``mark_in_progress(tenant_id, key) -> None``
    - ``clear_in_progress(tenant_id, key) -> None``
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

import redis

from apps.core.exceptions import IdempotencyConflict, IdempotencyInProgress
from apps.core.metrics import incr
from apps.core.models import IdempotencyRecord

logger = logging.getLogger(__name__)

# Redis key template for the in-progress sentinel.
# Namespaced by tenant_id so two tenants using the same idempotency key UUID
# do not collide.
_INPROGRESS_KEY_TEMPLATE = "idem:{tenant_id}:{key}"


def compute_request_hash(payload: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical JSON of ``payload``.

    Uses ``sort_keys=True`` and no extra whitespace so the hash is
    deterministic regardless of insertion order of the keys.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class AcquireOutcome(str, Enum):
    """Result of ``IdempotencyManager.check_for_replay``."""

    FRESH = "fresh"
    """No existing record — caller should proceed with the operation."""

    REPLAY = "replay"
    """An existing record with the same hash exists — caller should return
    the stored response without re-executing."""


@dataclass
class AcquireResult:
    """Outcome + optional stored response for REPLAY."""

    outcome: AcquireOutcome
    record: IdempotencyRecord | None = None


class IdempotencyManager:
    """Manages the two-phase idempotency check and sentinel lifecycle.

    Inject a ``redis.Redis`` client and the TTL constants in
    ``CheckoutService.__init__``; defaults fall back to the process-level
    singleton from ``apps.core.redis.get_redis_client()``.
    """

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        inprogress_ttl_s: int = 60,
    ) -> None:
        self._redis = redis_client
        self._inprogress_ttl_s = inprogress_ttl_s

    @property
    def _client(self) -> redis.Redis:
        if self._redis is None:
            from apps.core.redis import get_redis_client

            self._redis = get_redis_client()
        return self._redis

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_for_replay(
        self,
        tenant_id: UUID,
        key: UUID,
        request_hash: str,
    ) -> AcquireResult:
        """Check whether this (tenant, key) has been seen before.

        Call this *before* acquiring the distributed lock and before any DB
        write.  The caller must handle the three possible outcomes:

        - ``FRESH``  → proceed with checkout.
        - ``REPLAY`` → return ``result.record.response_body`` immediately
                       (HTTP status from ``result.record.response_status``).
        - Raises ``IdempotencyConflict`` or ``IdempotencyInProgress``.

        Args:
            tenant_id: UUID of the active tenant.
            key:       Client-supplied ``Idempotency-Key`` header value.
            request_hash: ``compute_request_hash(...)`` digest of the
                          request payload.

        Returns:
            ``AcquireResult(outcome=FRESH)`` or
            ``AcquireResult(outcome=REPLAY, record=...)``

        Raises:
            IdempotencyConflict:   Same key, different hash.
            IdempotencyInProgress: Same key, still being processed.
        """
        redis_key = _INPROGRESS_KEY_TEMPLATE.format(tenant_id=tenant_id, key=key)

        # 1. Check the durable Postgres record first (outside the Redis check)
        #    so a completed replay is returned even if the in-progress sentinel
        #    has already expired.
        try:
            record = IdempotencyRecord.objects.get(tenant_id=tenant_id, key=key)
            if record.request_hash != request_hash:
                logger.warning(
                    "idempotency.conflict",
                    extra={
                        "action": "idempotency.conflict",
                        "key": str(key),
                        "tenant_id": str(tenant_id),
                    },
                )
                incr("idempotency.conflict", tenant_id=str(tenant_id))
                raise IdempotencyConflict(
                    f"idempotency key {key} was used with a different request payload"
                )
            if record.status == IdempotencyRecord.Status.SUCCEEDED:
                logger.info(
                    "idempotency.replay",
                    extra={
                        "action": "idempotency.replay",
                        "key": str(key),
                        "tenant_id": str(tenant_id),
                    },
                )
                incr("idempotency.replay", tenant_id=str(tenant_id))
                return AcquireResult(outcome=AcquireOutcome.REPLAY, record=record)
            # status == IN_PROGRESS (rare — record committed but sentinel
            # already expired), fall through to the Redis check below.
        except IdempotencyRecord.DoesNotExist:
            pass

        # 2. Check the Redis in-progress sentinel.
        value = self._client.get(redis_key)
        if value is not None:
            logger.info(
                "idempotency.in_progress",
                extra={
                    "action": "idempotency.in_progress",
                    "key": str(key),
                    "tenant_id": str(tenant_id),
                },
            )
            incr("idempotency.in_progress", tenant_id=str(tenant_id))
            raise IdempotencyInProgress(
                f"idempotency key {key} is already being processed"
            )

        return AcquireResult(outcome=AcquireOutcome.FRESH)

    def mark_in_progress(self, tenant_id: UUID, key: UUID) -> None:
        """Set the Redis in-progress sentinel for ``(tenant_id, key)``.

        Uses ``NX`` so a concurrent call that beat us to it returns False.
        The caller should treat a False return as ``IdempotencyInProgress``.

        Raises:
            IdempotencyInProgress: If the sentinel is already set (a concurrent
                request snuck in between ``check_for_replay`` and this call).
        """
        redis_key = _INPROGRESS_KEY_TEMPLATE.format(tenant_id=tenant_id, key=key)
        set_ok = self._client.set(
            redis_key,
            "in_progress",
            nx=True,
            ex=self._inprogress_ttl_s,
        )
        if not set_ok:
            raise IdempotencyInProgress(
                f"idempotency key {key} is already being processed"
            )

    def clear_in_progress(self, tenant_id: UUID, key: UUID) -> None:
        """Remove the Redis in-progress sentinel.

        Called from the ``finally`` block of the checkout critical section so
        the client may retry after a transient failure.  Errors are logged but
        not raised — the sentinel will expire via its TTL regardless.
        """
        redis_key = _INPROGRESS_KEY_TEMPLATE.format(tenant_id=tenant_id, key=key)
        try:
            self._client.delete(redis_key)
        except Exception:
            logger.exception(
                "Failed to clear idempotency sentinel for key %s — it will "
                "expire via TTL (%ds).",
                key,
                self._inprogress_ttl_s,
            )
