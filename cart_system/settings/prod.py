"""Production settings.

Hardened defaults: DEBUG off, strict hosts, secure cookies, HSTS, structured
logging only. Every secret comes from the environment — no fallbacks.
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .base import env

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

DEBUG = False

# In prod the env var MUST be set; raise loudly otherwise.
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

# ---------------------------------------------------------------------------
# Security headers (PROJECT_SPEC §5.1, §6 — Engineering Standards)
# ---------------------------------------------------------------------------

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env("DJANGO_SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 30)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"

# ---------------------------------------------------------------------------
# Logging — JSON output for machine-readable log ingestion in production.
# ---------------------------------------------------------------------------
# Override only the console handler's formatter to "json" so every log line
# is a single compact JSON object ready for Datadog / CloudWatch / Loki
# without any parsing configuration.  The filter and root logger level are
# inherited unchanged from base.py.

LOGGING["handlers"]["console"]["formatter"] = "json"  # noqa: F405

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
# The deployment is expected to serve ``STATIC_ROOT`` via a CDN or a frontend
# proxy. We do not configure WhiteNoise here to keep the runtime minimal —
# add it explicitly when needed.
