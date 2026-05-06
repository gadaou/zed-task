"""Distributed Redis lock primitives.

Implements the PROJECT_SPEC §4.4 locking contract:

- ``SET key token NX PX ttl_ms`` — acquire with a fenced token (UUIDv4).
- Release via a Lua script that atomically checks the token before deleting
  so only the holder can release (fenced lock).
- Locks are advisory, not authoritative.  The DB row lock + version column
  is the actual safety net; Redis locks are a coordination optimisation.

Usage::

    from apps.core.locks import redis_lock
    from apps.core.exceptions import LockNotAcquired

    try:
        with redis_lock(f"lock:checkout:{tenant_id}:{cart_id}", ttl_ms=15000):
            # critical section
            ...
    except LockNotAcquired:
        raise CheckoutLocked(...)

The context manager guarantees the lock is released in ``__exit__`` even if
the body raises.  Release errors (e.g. Redis is briefly unreachable) are
logged at ERROR level but never re-raised so the original exception is
preserved.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Generator

import redis

from apps.core.exceptions import LockNotAcquired

logger = logging.getLogger(__name__)

# Canonical Lua script for fenced release.
# Atomically: if the stored value equals our token, delete the key;
# otherwise do nothing.  Returns 1 on successful release, 0 if the token
# did not match (lock expired or was released by another path).
_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


@contextmanager
def redis_lock(
    key: str,
    ttl_ms: int,
    *,
    client: redis.Redis | None = None,
    token: str | None = None,
) -> Generator[str, None, None]:
    """Acquire a fenced Redis lock for the duration of the ``with`` block.

    Args:
        key: The Redis key for the lock.  Should be namespaced by tenant and
            resource, e.g. ``lock:checkout:{tenant_id}:{cart_id}``.
        ttl_ms: Lock TTL in milliseconds.  Must be sized to the longest
            expected critical section plus a safety margin.
        client: The Redis client to use.  Defaults to
            ``apps.core.redis.get_redis_client()``.
        token: The fenced token stored as the lock value.  If omitted a
            UUIDv4 is generated per acquire.

    Yields:
        The token string that was stored, in case the caller needs it.

    Raises:
        LockNotAcquired: The key was already set (another process holds the
            lock). No lock is held on raise; no cleanup needed.
    """
    from apps.core.redis import get_redis_client

    _client = client or get_redis_client()
    _token = token or str(uuid.uuid4())

    acquired = _client.set(key, _token, nx=True, px=ttl_ms)
    if not acquired:
        raise LockNotAcquired(
            f"could not acquire lock '{key}' — another process holds it"
        )

    try:
        yield _token
    finally:
        try:
            # Use eval() directly (not register_script/evalsha) so the Lua
            # script works with both real Redis and fakeredis in tests.
            _client.eval(_RELEASE_LUA, 1, key, _token)
        except Exception:
            # Release errors (Redis unreachable, network blip) must not mask
            # the original exception from the critical section.  The lock will
            # expire on its own via the TTL; the DB row lock is the safety net.
            logger.exception(
                "Failed to release Redis lock '%s' — it will expire via TTL.",
                key,
            )
