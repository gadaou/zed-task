"""Core models.

Shared cross-cutting abstractions:
- ``IdempotencyRecord`` — durable idempotency store for the checkout endpoint.

PROJECT_SPEC §4.5: "An idempotency_record(tenant_id, key, request_hash,
response_status, response_body, created_at) table with a unique index on
(tenant_id, key)."
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


class IdempotencyRecord(models.Model):
    """Durable record of a completed (or in-flight) idempotent request.

    Written *inside* the checkout transaction at commit time so that a
    transaction rollback also removes the record — no orphaned "succeeded"
    records for orders that were never created.

    Lifecycle:
        1. Before BEGIN: Redis ``SET NX`` marks the key ``in_progress``.
        2. Inside transaction, on success: this row is INSERTed with
           ``status=SUCCEEDED`` and the serialised response.
        3. On COMMIT: the Redis in-progress sentinel is cleared by the
           service's ``finally`` block.
        4. On ROLLBACK: this row is never committed; the Redis sentinel is
           cleared; the client may retry.

    Replay path:
        1. ``IdempotencyManager.acquire`` checks Redis for the in-progress
           sentinel; if absent, queries this table.
        2. Matching (tenant, key) row with same hash → return stored response.
        3. Matching row with different hash → raise ``IdempotencyConflict``.

    The ``tenant`` field is a plain UUIDField (not a FK) so this table is
    usable without a full tenant context — specifically by admin tooling that
    sweeps expired records.
    """

    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Tenant identifier as a UUID; not a FK so this table is independently
    # queryable by the sweep job without a tenant context.
    tenant_id = models.UUIDField(db_index=True)

    # Client-supplied idempotency key (UUID).
    key = models.UUIDField()

    # SHA-256 hex digest of the canonical JSON of the request payload.
    # Used to detect "same key + different payload" conflicts.
    request_hash = models.CharField(max_length=64)

    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.IN_PROGRESS,
    )

    # HTTP status code of the stored response; null until the request
    # completes.  PositiveSmallIntegerField covers the 1xx–5xx range.
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)

    # Serialised response body (the JSON dict the view returned); null until
    # the request completes.
    response_body = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Idempotency Record"
        verbose_name_plural = "Idempotency Records"
        constraints = [
            # One record per (tenant, key) — the fundamental uniqueness
            # guarantee for idempotent replays.
            models.UniqueConstraint(
                fields=["tenant_id", "key"],
                name="uq_idempotency_tenant_key",
            ),
            models.CheckConstraint(
                check=Q(status__in=["IN_PROGRESS", "SUCCEEDED", "FAILED"]),
                name="ck_idempotency_status_valid",
            ),
        ]
        indexes = [
            # Primary access pattern: "look up the record for this tenant + key."
            models.Index(
                fields=["tenant_id", "key"],
                name="ix_idempotency_tenant_key",
            ),
            # Sweep job: "find SUCCEEDED records older than N hours."
            models.Index(
                fields=["status", "created_at"],
                name="ix_idempotency_status_created",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"IdempotencyRecord tenant={self.tenant_id} key={self.key} [{self.status}]"
        )
