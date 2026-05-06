"""Test settings.

Goals: fast, deterministic, isolated. Postgres remains the integration target
(PROJECT_SPEC §3.1) but unit-test runs use a lightweight in-memory engine
when the developer has not yet provisioned Postgres locally.
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False
SECRET_KEY = "test-only-not-a-secret"
ALLOWED_HOSTS = ["*"]

# Use the configured Postgres if available, else fall back to in-memory SQLite
# so a fresh checkout can run unit tests without infra.
DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default="sqlite:///:memory:",
    ),
}

# Faster password hashing in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Quiet down logging during tests.
LOGGING["root"]["level"] = "WARNING"  # noqa: F405

# ---------------------------------------------------------------------------
# Celery — run tasks eagerly in tests so no broker is needed.
# CELERY_TASK_ALWAYS_EAGER=True makes every apply_async() call synchronous
# and inline, with no Redis touch.  Tests that need to assert "task was NOT
# dispatched on rollback" must patch the dispatcher directly.
# ---------------------------------------------------------------------------
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# ---------------------------------------------------------------------------
# Redis — tests monkeypatch apps.core.redis.get_redis_client() via the
# ``redis_client`` fixture in apps/order/tests/conftest.py to return a
# fakeredis.FakeRedis instance.  The URL here is a placeholder; the actual
# connection is never established during tests.
# ---------------------------------------------------------------------------
REDIS_URL = "redis://localhost:6379/0"  # overridden by monkeypatch in tests
