"""Development settings.

Optimised for local iteration: DEBUG on, browsable API renderer enabled,
permissive CORS-friendly hosts. Never use this module in production.
"""

from __future__ import annotations

from .base import *  # noqa: F401,F403
from .base import REST_FRAMEWORK, env

# ---------------------------------------------------------------------------
# Core overrides
# ---------------------------------------------------------------------------

DEBUG = True

# A development-only fallback secret keeps ``manage.py runserver`` working
# without a ``.env`` file. The variable is still respected if set.
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="django-insecure-dev-only-do-not-use-in-production",
)

ALLOWED_HOSTS = env(
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", "0.0.0.0"],
)

INTERNAL_IPS = ["127.0.0.1"]

# ---------------------------------------------------------------------------
# DRF dev tweaks
# ---------------------------------------------------------------------------
# Add the browsable API in development for quick exploration.

REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
}

# ---------------------------------------------------------------------------
# Email — print to console in dev
# ---------------------------------------------------------------------------

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
