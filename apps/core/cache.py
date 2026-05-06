"""Cart read-through cache utilities.

Implements the cart caching layer described in PROJECT_SPEC §5.2 (hot-path
caching) and §5.1 (graceful degradation).  Redis is the cache backend;
every function soft-fails on Redis errors so a Redis outage never breaks
cart reads — the service simply falls through to Postgres.

Key design rules
----------------
- One canonical cache key per (tenant_id, user_id): ``cart:read:{t}:{u}``.
  Stable across cart versions so an explicit DEL always hits the right entry.
- Invalidation fires via ``transaction.on_commit`` — never before the DB
  transaction that produced the new state has been committed.  A rollback
  therefore leaves the cached entry untouched (correct: the mutation never
  happened).
- Short TTL (default 60s, configurable via ``CART_CACHE_TTL_S``) as a
  belt-and-suspenders guard in case a Redis DEL is lost during a blip.
- ``CART_CACHE_ENABLED = False`` lets ops kill caching globally via env var
  without a redeploy.

Public API
----------
- ``cart_read_cache_key(tenant_id, user_id) -> str``
- ``get_cart_cache(tenant_id, user_id) -> dict | None``
- ``set_cart_cache(tenant_id, user_id, payload, ttl_s=None) -> None``
- ``invalidate_cart_cache(tenant_id, user_id) -> None``
- ``schedule_cart_cache_invalidation(tenant_id, user_id) -> None``
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_client():
    """Return the shared Redis client from the process-level singleton.

    Imported lazily so tests that monkeypatch ``apps.core.redis.get_redis_client``
    have already applied the patch before this code runs.
    """
    from apps.core.redis import get_redis_client

    return get_redis_client()


def _is_enabled() -> bool:
    return bool(getattr(settings, "CART_CACHE_ENABLED", True))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cart_read_cache_key(tenant_id: UUID, user_id: UUID) -> str:
    """Return the canonical Redis key for the cart read cache entry.

    Namespaced by ``tenant_id`` to prevent cross-tenant collisions and by
    ``user_id`` because each user has exactly one ACTIVE cart per tenant.

    Format: ``cart:read:{tenant_id}:{user_id}``
    """
    return f"cart:read:{tenant_id}:{user_id}"


def get_cart_cache(tenant_id: UUID, user_id: UUID) -> dict | None:
    """Return the cached cart payload or ``None`` on miss / error.

    Returns ``None`` when:
    - Caching is disabled (``CART_CACHE_ENABLED = False``).
    - The key does not exist in Redis (cache miss).
    - The stored value is not valid JSON (corrupt entry — treated as miss).
    - Redis is unavailable (network error, timeout, etc.).

    The caller must treat ``None`` as "fetch from DB".
    """
    if not _is_enabled():
        return None

    key = cart_read_cache_key(tenant_id, user_id)
    try:
        raw = _get_client().get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("cart cache: corrupt JSON for key %s — treating as miss", key)
        return None
    except Exception:
        logger.exception("cart cache: GET failed for key %s — falling back to DB", key)
        return None


def set_cart_cache(
    tenant_id: UUID,
    user_id: UUID,
    payload: dict,
    ttl_s: int | None = None,
) -> None:
    """Store ``payload`` in the cart read cache with a TTL.

    Args:
        tenant_id: UUID of the active tenant.
        user_id:   UUID of the customer.
        payload:   Serialised cart dict (output of ``CartReadSerializer``).
        ttl_s:     TTL in seconds.  Defaults to ``settings.CART_CACHE_TTL_S``
                   (env-configurable, default 60).

    Silently no-ops if caching is disabled or if Redis is unavailable.
    """
    if not _is_enabled():
        return

    if ttl_s is None:
        ttl_s = getattr(settings, "CART_CACHE_TTL_S", 60)

    key = cart_read_cache_key(tenant_id, user_id)
    try:
        _get_client().set(key, json.dumps(payload), ex=ttl_s)
    except Exception:
        logger.exception("cart cache: SET failed for key %s — cache will be empty", key)


def invalidate_cart_cache(tenant_id: UUID, user_id: UUID) -> None:
    """Delete the cached cart entry for ``(tenant_id, user_id)``.

    Safe to call from ``transaction.on_commit`` callbacks.  Silently no-ops
    on a missing key or Redis error — the TTL will evict the entry eventually.
    """
    if not _is_enabled():
        return

    key = cart_read_cache_key(tenant_id, user_id)
    try:
        _get_client().delete(key)
    except Exception:
        logger.exception(
            "cart cache: DEL failed for key %s — entry will expire via TTL", key
        )


def schedule_cart_cache_invalidation(tenant_id: UUID, user_id: UUID) -> None:
    """Register a post-commit cache invalidation for ``(tenant_id, user_id)``.

    Must be called **inside** a ``transaction.atomic()`` block.  The actual
    Redis ``DEL`` is deferred to ``transaction.on_commit`` so:

    - A DB rollback leaves the cached entry untouched (the mutation never
      happened, so the cached ACTIVE cart is still correct).
    - A successful COMMIT guarantees the cache is purged *after* the new
      state is durable — readers that miss immediately after the commit
      will fetch the updated row from Postgres.

    This is the preferred entry-point for all service-layer callers.
    """
    # Capture UUIDs by value in the closure so they cannot be mutated later.
    _tenant_id = tenant_id
    _user_id = user_id
    transaction.on_commit(lambda: invalidate_cart_cache(_tenant_id, _user_id))
