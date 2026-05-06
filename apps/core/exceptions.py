"""Core domain exceptions.

Implements the cross-cutting entries of the PROJECT_SPEC §2 error taxonomy
that are not app-specific: distributed-lock failures, idempotency errors.

Design rules (mirrors apps/coupon/exceptions.py):
- Every exception inherits from ``CoreDomainError`` for uniform ``except``
  in view-layer handlers.
- ``type`` matches the PROJECT_SPEC §2 taxonomy verbatim.
- ``detail`` is safe-for-logs; it must never carry PII or secrets.
"""

from __future__ import annotations

from typing import Any


class CoreDomainError(Exception):
    """Base class for core cross-cutting domain errors."""

    type: str = "core/error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        self.detail = detail or self.__class__.__doc__ or ""
        self.extra = extra
        super().__init__(self.detail)


class LockNotAcquired(CoreDomainError):
    """The distributed lock could not be acquired — another process holds it.

    Maps to ``cart/locked`` in the PROJECT_SPEC §2 error taxonomy because the
    client-visible semantics are "the cart is currently locked by a concurrent
    checkout".
    """

    type = "cart/locked"


class IdempotencyConflict(CoreDomainError):
    """The Idempotency-Key has been used with a different request payload.

    PROJECT_SPEC §4.5: "Replay with same key + different request hash:
    409 idempotency/conflict."
    """

    type = "idempotency/conflict"


class IdempotencyInProgress(CoreDomainError):
    """A request with the same Idempotency-Key is currently being processed.

    PROJECT_SPEC §4.5: "Replay while in_progress: 409 idempotency/in-progress
    (client should poll or back off)."
    """

    type = "idempotency/in-progress"
