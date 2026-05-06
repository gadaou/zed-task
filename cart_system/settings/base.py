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
    REDIS_URL=(str, "redis://localhost:6379/0"),
    CELERY_BROKER_URL=(str, "redis://localhost:6379/1"),
    CELERY_RESULT_BACKEND=(str, "redis://localhost:6379/2"),
    CHECKOUT_LOCK_TTL_MS=(int, 15000),
    IDEMPOTENCY_INPROGRESS_TTL_S=(int, 60),
    IDEMPOTENCY_RECORD_TTL_HOURS=(int, 24),
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
    "apps.invoice",
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
    "/health/",
    "/ready/",
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
# Media files — invoice PDFs are stored here under media/invoices/
# PROJECT_SPEC §2 bonus: Invoice handling.
# ---------------------------------------------------------------------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

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
    # ---------------------------------------------------------------------------
    # API identity
    # ---------------------------------------------------------------------------
    "TITLE": "cart_system API",
    "VERSION": "1.0.0",
    "DESCRIPTION": (
        "## Multi-tenant shopping cart and checkout API\n\n"
        "cart_system serves thousands of merchant tenants on a single Django/Postgres "
        "cluster. Every request is scoped to a single tenant via the "
        "`X-Tenant-Domain` header — supply it on **every** call.\n\n"
        "### Idempotency\n\n"
        "Mutating endpoints (checkout, …) require an `Idempotency-Key: <uuid>` header. "
        "Repeating the exact same request (same key **and** same body) is safe — the "
        "server stores the first response in Postgres and returns it verbatim without "
        "re-executing side-effects. Using the same key with a *different* body returns "
        "`409 idempotency/conflict`.\n\n"
        "### Error format\n\n"
        "All error responses follow [RFC 7807](https://www.rfc-editor.org/rfc/rfc7807) "
        "`application/problem+json`:\n\n"
        "```json\n"
        '{"type": "https://cart-system.local/problems/cart/empty",\n'
        ' "title": "Cart is empty",\n'
        ' "status": 422,\n'
        ' "detail": "cart abc… has no items"}\n'
        "```\n\n"
        "### Concurrency guarantees\n\n"
        "- **Checkout serialisation**: a Redis distributed lock "
        "(`lock:checkout:{tenant}:{cart}`) ensures at most one checkout executes "
        "for a given cart at a time. Concurrent attempts receive `409 cart/locked`.\n"
        "- **Stock safety**: stock deduction uses `UPDATE … WHERE stock >= qty` — "
        "zero rows means out-of-stock, never negative stock.\n"
        "- **No double orders**: the `IdempotencyRecord` unique constraint on "
        "`(tenant_id, idempotency_key)` is written *inside* the checkout DB "
        "transaction, making duplicate orders structurally impossible.\n\n"
        "### Payment lifecycle\n\n"
        "`POST /carts/{cart_id}/checkout` → `202 Accepted` (payment pending) → "
        "async Celery task → gateway → `AUTHORIZED` or `FAILED`."
    ),
    "CONTACT": {
        "name": "Platform Team",
        "email": "platform@cart-system.local",
    },
    "LICENSE": {
        "name": "Proprietary",
    },
    "EXTERNAL_DOCS": {
        "description": "Full architecture and project spec",
        "url": "https://github.com/cart-system/docs/blob/main/PROJECT_SPEC.md",
    },
    # ---------------------------------------------------------------------------
    # Schema generation
    # ---------------------------------------------------------------------------
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v[0-9]+",
    "COMPONENT_SPLIT_REQUEST": True,

    # Enumerate all tags so they appear in sidebar order (not alphabetical).
    # Each tag gets a description that surfaces in the SwaggerUI sidebar.
    "TAGS": [
        {
            "name": "Checkout",
            "description": (
                "Checkout a shopping cart. Accepts payment method, shipping address, "
                "and an idempotency key. Runs under a Redis distributed lock + Postgres "
                "row lock to guarantee exactly-once order creation. Returns `202 Accepted` "
                "immediately; the actual payment authorisation happens asynchronously via "
                "the Celery `payments` queue."
            ),
        },
        {
            "name": "Cart",
            "description": (
                "Manage the shopping cart — add items, remove items, apply/remove coupons, "
                "and inspect the current totals. Every cart is scoped to a single tenant "
                "and customer (`user_id`). Cart state transitions: `ACTIVE → CHECKED_OUT`."
            ),
        },
        {
            "name": "Coupon",
            "description": (
                "Manage discount coupons for a tenant. Coupons are either **percentage** "
                "or **fixed-amount**. Constraints (minimum cart value, country allowlist, "
                "usage limits, validity window) are evaluated at apply-time and re-validated "
                "at checkout."
            ),
        },
        {
            "name": "Payment",
            "description": (
                "Inspect and manage payment records and stored payment methods. "
                "Payment status follows the FSM: `REQUIRES_CONFIRMATION → AUTHORIZED → "
                "CAPTURED → SUCCEEDED` (or `FAILED / CANCELLED / REFUNDED`)."
            ),
        },
        {
            "name": "Catalog",
            "description": "Product catalog — prices, stock levels, and metadata.",
        },
        {
            "name": "Addresses",
            "description": (
                "Manage customer shipping addresses. Soft-deleted addresses are retained "
                "for order-history integrity. At most one address may be the default per "
                "customer within a tenant."
            ),
        },
        {
            "name": "Tenant",
            "description": "Tenant provisioning and management (ops/admin use only).",
        },
    ],

    # ---------------------------------------------------------------------------
    # Security schemes
    # ---------------------------------------------------------------------------
    # X-Tenant-Domain is a custom header required on every non-exempt endpoint.
    # It is not a standard OAuth2/Bearer scheme, so we model it as an apiKey.
    # APPEND_COMPONENTS injects the definition into the generated schema's
    # ``components.securitySchemes`` block.
    "SECURITY": [{"TenantDomain": []}],
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "TenantDomain": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Tenant-Domain",
                "description": (
                    "The tenant's unique domain (e.g. `acme.mysaas.com`). "
                    "Required on every non-exempt endpoint. "
                    "Missing → `400 tenant/missing-header`. "
                    "Unknown domain → `404 tenant/not-found`. "
                    "Inactive tenant → `403 tenant/disabled`."
                ),
            },
        }
    },

    # ---------------------------------------------------------------------------
    # SwaggerUI / Redoc configuration
    # ---------------------------------------------------------------------------
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "persistAuthorization": True,
        "displayRequestDuration": True,
        "filter": True,
        "defaultModelsExpandDepth": 2,
        "defaultModelExpandDepth": 2,
        "docExpansion": "list",
        "syntaxHighlight.theme": "obsidian",
        "tryItOutEnabled": True,
    },
    "REDOC_UI_SETTINGS": {
        "hideDownloadButton": False,
        "expandResponses": "200,202",
        "pathInMiddlePanel": True,
        "theme": {
            "colors": {"primary": {"main": "#2C6FED"}},
            "typography": {"fontSize": "15px"},
        },
    },

    # ---------------------------------------------------------------------------
    # Schema quality
    # ---------------------------------------------------------------------------
    # Postprocess hooks for consistency across all generated schemas.
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
    ],
    "ENUM_GENERATE_CHOICE_DESCRIPTION": True,
    "ENUM_ADD_EXPLICIT_BLANK_NULL_CHOICE": False,
    "SORT_OPERATIONS": False,  # preserve the natural URL order defined in urls.py

    # Show full request/response examples in the UI.
    "SERVE_AUTHENTICATION": [],
}

# ---------------------------------------------------------------------------
# Redis — coordination layer (locks, rate limits, idempotency, Celery broker)
# PROJECT_SPEC §4.4 (distributed locking) and §4.6 (async workers).
# ---------------------------------------------------------------------------

REDIS_URL: str = env("REDIS_URL")

# ---------------------------------------------------------------------------
# Celery — async workers for payments, invoices, notifications
# PROJECT_SPEC §4.6. Three named queues so slow gateways cannot block invoice
# generation. Configuration uses the "CELERY_" namespace prefix so every
# CELERY_* env var in django.conf.settings is picked up by app.conf.
# ---------------------------------------------------------------------------

CELERY_BROKER_URL: str = env("CELERY_BROKER_URL")
CELERY_RESULT_BACKEND: str = env("CELERY_RESULT_BACKEND")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# ---------------------------------------------------------------------------
# Checkout / Idempotency constants  (PROJECT_SPEC §4.4, §4.5)
# ---------------------------------------------------------------------------

# Redis lock TTL for the checkout critical section (milliseconds).
# Sized to: gateway timeout (8s) + DB budget (2s) + safety margin (5s) = 15s.
CHECKOUT_LOCK_TTL_MS: int = env("CHECKOUT_LOCK_TTL_MS")

# How long the Redis "in_progress" idempotency sentinel lives (seconds).
# A retry within this window gets 409 idempotency/in-progress rather than
# starting a second checkout.
IDEMPOTENCY_INPROGRESS_TTL_S: int = env("IDEMPOTENCY_INPROGRESS_TTL_S")

# How long durable IdempotencyRecord DB rows are kept before the sweep job
# deletes them (hours).  PROJECT_SPEC §4.5 mandates 24h.
IDEMPOTENCY_RECORD_TTL_HOURS: int = env("IDEMPOTENCY_RECORD_TTL_HOURS")

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
