"""Core Redis client factory.

Provides a single ``get_redis_client()`` function that returns a configured
``redis.Redis`` instance.  The module is intentionally thin so tests can
monkeypatch it cleanly:

    monkeypatch.setattr("apps.core.redis.get_redis_client", lambda: fake_redis)

PROJECT_SPEC §4.4: we use ``redis-py`` directly rather than the django-redis
cache backend so the checkout and lock code can depend on a plain ``Redis``
object with a stable, well-understood API.
"""

from __future__ import annotations

from functools import lru_cache

import redis
from django.conf import settings


@lru_cache(maxsize=1)
def get_redis_client() -> redis.Redis:
    """Return the shared Redis client, created once per process.

    ``decode_responses=True`` — all keys and values are str, not bytes.
    This matches the lock key format (``lock:checkout:{tenant}:{cart}``) and
    the idempotency key format throughout the codebase.

    The ``lru_cache`` makes this a process-level singleton.  Tests that need
    an isolated client should monkeypatch this function rather than calling
    ``get_redis_client.cache_clear()`` (which would break concurrent tests
    in the same process).
    """
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
