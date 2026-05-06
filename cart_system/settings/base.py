"""Base settings shared across all environments.

Environment-specific settings (``dev``, ``prod``, ``test``) import from this
module and override the bits that differ. Anything that is identical
everywhere lives here.

Configuration is read from environment variables via ``django-environ``.
A ``.env`` file is loaded in development (see ``.env.example`` at the repo
root for the full list of variables).

Cross-references throughout this file:
* PROJECT_SPEC.md §3   — Constraints (single Postgres, tenant isolation)
* PROJECT_SPEC.md §4   — Architectural Principles (DRF + service layer)
* PROJECT_SPEC.md §5.4 — API design (versioning, problem+json, OpenAPI)
* PROJECT_SPEC.md §6.3 — Structured logging
"""

from __future__ import annotations

from pathlib import Path

import environ

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# ``BASE_DIR`` points to the repository root (one level above ``cart_system/``).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
    DJANGO_SECURE_SSL_REDIRECT=(bool, False),
    DJANGO_TIME_ZONE=(str, "UTC"),
)

# Load ``.env`` if present. In production the env vars are injected by the
# orchestrator and this is a no-op.
_dotenv_path = BASE_DIR / ".env"
if _dotenv_path.exists():
    environ.Env.read_env(str(_dotenv_path))

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
# ``SECRET_KEY``, ``DEBUG``, and ``ALLOWED_HOSTS`` are intentionally NOT set
# here. Each environment-specific module (``dev``, ``prod``, ``test``) owns
# its own policy: dev provides a safe insecure fallback, prod requires the
# env var with no fallback, test uses a fixed literal. This keeps base.py
# importable in any context without surprising failures.

ROOT_URLCONF = "cart_system.urls"
WSGI_APPLICATION = "cart_system.wsgi.application"
ASGI_APPLICATION = "cart_system.asgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
]

# Local apps live under ``apps/`` and are imported via the dotted path
# ``apps.<name>``. Each app's ``AppConfig`` declares an explicit ``label``
# so DB tables are not prefixed with ``apps_``.
LOCAL_APPS = [
    "apps.core",
    "apps.tenant",
    "apps.catalog",
    "apps.cart",
    "apps.coupon",
    "apps.addresses",
    "apps.payment",
    "apps.order",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # TenantMiddleware runs immediately after SecurityMiddleware so every
    # downstream middleware and view already has request.tenant populated.
    # PROJECT_SPEC §4.2.
    "apps.tenant.middleware.TenantMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Paths that bypass TenantMiddleware resolution (PROJECT_SPEC §4.2).
# Any path whose prefix matches an entry here skips the X-Tenant-Domain
# requirement.  Override in environment-specific settings if needed.
TENANT_EXEMPT_PATHS: list[str] = [
    "/admin/",
    "/healthz",
    "/readyz",
    "/api/schema/",
    "/api/docs/",
    "/api/redoc/",
]

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database — PostgreSQL via DATABASE_URL (PROJECT_SPEC §3.1)
# ---------------------------------------------------------------------------
# A single logical Postgres database is the system of record for every tenant.
# In tests we override this to a faster engine in ``settings/test.py``.

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default="postgres://cart_user:cart_pass@localhost:5432/cart_system",
    ),
}
DATABASES["default"].setdefault("CONN_MAX_AGE", 60)
DATABASES["default"].setdefault("ATOMIC_REQUESTS", False)

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------------------
# Django REST Framework (PROJECT_SPEC §4 / §5.4)
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.CursorPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "user": "1000/hour",
        "anon": "100/hour",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # The custom problem+json exception handler (RFC 7807) lands with apps.core
    # in a later iteration; until then the DRF default is used so behaviour is
    # predictable and lint-clean.
    # "EXCEPTION_HANDLER": "apps.core.exceptions.problem_json_handler",
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%S.%fZ",
}

# ---------------------------------------------------------------------------
# drf-spectacular (OpenAPI 3.1)
# ---------------------------------------------------------------------------

SPECTACULAR_SETTINGS = {
    "TITLE": "cart_system API",
    "DESCRIPTION": "Multi-tenant cart and checkout API. See PROJECT_SPEC.md.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v[0-9]+",
    "COMPONENT_SPLIT_REQUEST": True,
}

# ---------------------------------------------------------------------------
# Logging — structured JSON logs with bound context (PROJECT_SPEC §6.3)
# ---------------------------------------------------------------------------
# A full structlog pipeline lands with apps.core. For now we ship a sane
# console logger that production can override.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": env("DJANGO_DJANGO_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
}
