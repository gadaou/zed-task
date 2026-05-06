"""Core models.

Reserved for cross-cutting abstractions: ``UUIDPrimaryKeyMixin``,
``TimestampedMixin``, ``SoftDeleteMixin``, ``IdempotencyRecord``.

PROJECT_SPEC §6.2 (UUID primary keys), §4.5 (idempotency).
"""

from django.db import models  # noqa: F401  (kept for forthcoming abstract bases)
