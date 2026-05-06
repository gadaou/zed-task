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
