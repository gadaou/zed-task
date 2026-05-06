# cart_system

Multi-tenant cart and checkout service. Single Django + DRF deployment, single PostgreSQL database, strict tenant isolation, pluggable payment gateways.

The architectural contract for the system is [`PROJECT_SPEC.md`](PROJECT_SPEC.md). Every change in this repository should trace back to a section in that document.

---

## Status

Foundation iteration — the project skeleton, settings split, six domain apps, and DRF wiring are in place. Models, services, API endpoints, Celery, Redis, and tests land in subsequent iterations driven off the spec.

---

## Prerequisites

| Tool                   | Version  | Notes                                              |
| ---------------------- | -------- | -------------------------------------------------- |
| Python                 | 3.11+    | 3.11 or 3.12 supported                              |
| PostgreSQL             | 14+      | 16 recommended; the only system of record           |
| pip                    | 23+      | bundled with modern Python                          |
| Make / shell           | any      | optional convenience                                |

A POSIX shell, `git`, and a working C toolchain (for psycopg's binary wheel fallback) are assumed.

---

## Quick start

```bash
# 1. Clone and enter
git clone <repo-url> cart_system && cd cart_system

# 2. Create a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
#   then edit .env — at minimum set DATABASE_URL to a Postgres you control

# 5. Create the database (once)
createdb cart_system        # or use your own provisioning

# 6. Run migrations
python manage.py migrate

# 7. Create a superuser for /admin (optional)
python manage.py createsuperuser

# 8. Run the dev server
python manage.py runserver
```

Visit:

* `http://localhost:8000/healthz` — liveness check, returns `{"status":"ok"}`.
* `http://localhost:8000/api/docs/` — Swagger UI (OpenAPI 3.1).
* `http://localhost:8000/api/redoc/` — Redoc rendering.
* `http://localhost:8000/api/schema/` — raw OpenAPI document.
* `http://localhost:8000/admin/` — Django admin.

---

## Settings

Settings are split per environment. Pick one with `DJANGO_SETTINGS_MODULE`:

| Module                          | Purpose                                       | Default for                |
| ------------------------------- | --------------------------------------------- | -------------------------- |
| `cart_system.settings.base`     | Shared base. Never imported directly outside this package. | n/a               |
| `cart_system.settings.dev`      | Local development. `DEBUG=True`, browsable API, `AllowAny`. | `manage.py`         |
| `cart_system.settings.prod`     | Hardened production: HSTS, secure cookies, no fallbacks.    | `wsgi.py`, `asgi.py`|
| `cart_system.settings.test`     | Fast deterministic test runs. SQLite fallback if no Postgres. | pytest config      |

Configuration is environment-driven via [`django-environ`](https://django-environ.readthedocs.io). See [`.env.example`](.env.example) for the full list of variables.

---

## Repository layout

```text
.
├── PROJECT_SPEC.md              # source of truth — read this first
├── README.md
├── manage.py
├── requirements.txt
├── .env.example
├── .gitignore
├── cart_system/                 # project package
│   ├── __init__.py
│   ├── asgi.py
│   ├── wsgi.py
│   ├── urls.py                  # root router: /admin, /healthz, /api/v1/, /api/docs/
│   └── settings/
│       ├── __init__.py
│       ├── base.py
│       ├── dev.py
│       ├── prod.py
│       └── test.py
└── apps/                        # local Django apps (referenced as apps.<name>)
    ├── core/                    # cross-cutting: health, IDs, idempotency, errors
    ├── tenant/                  # Tenant model, middleware, manager
    ├── cart/                    # Cart, CartItem, cart operations
    ├── coupon/                  # Coupon, constraints, redemption ledger
    ├── payment/                 # Methods, intents, gateway plug-in registry
    └── order/                   # Order, OrderItem, Address, Invoice
```

Each app follows the same internal layout:

```text
apps/<name>/
├── __init__.py
├── apps.py             # AppConfig
├── models.py           # ORM models
├── admin.py            # admin registrations
├── urls.py             # router (mounted under /api/v1/<resource>/)
├── views.py            # thin DRF views — parse, validate, dispatch, serialize
├── serializers.py      # input validation + output shaping
├── services.py         # all business logic, transactions, locks
├── migrations/
└── tests/
```

This separation between `views`, `services`, and `models` is mandated by [PROJECT_SPEC §4.1](PROJECT_SPEC.md). Views must stay thin; business logic lives in `services.py`.

---

## Running tests

```bash
# Once pytest is added (next iteration), the canonical command is:
DJANGO_SETTINGS_MODULE=cart_system.settings.test pytest

# Today, Django's own test runner works:
python manage.py test
```

Coverage targets (PROJECT_SPEC §6.5):

* Overall: 85%+
* `services/checkout.py`, `services/coupons.py`, `services/payments.py`: 95%+
* Every `PaymentGateway` implementation: 100% against the shared contract test base.

---

## Common commands

```bash
# Run the full Django check pipeline
python manage.py check

# Generate the OpenAPI schema to a file
python manage.py spectacular --file openapi.yaml

# Format / lint (ruff + black added in a later iteration)
# ruff check .
# black .

# Production-style server (gunicorn, defaults to settings.prod)
DJANGO_SETTINGS_MODULE=cart_system.settings.prod \
DJANGO_SECRET_KEY=... DJANGO_ALLOWED_HOSTS=example.com DATABASE_URL=... \
gunicorn cart_system.wsgi:application --workers 4 --bind 0.0.0.0:8000
```

---

## API conventions

* Versioned: every business endpoint lives under `/api/v1/`. New major versions go to `/api/v2/`; v1 stays supported for at least 12 months.
* Resource-oriented: `/api/v1/carts/{cart_id}/items/{item_id}` style. No verbs in paths except for true actions like `/checkout`.
* Errors: RFC 7807 problem+json with stable `type` URIs (the taxonomy is enumerated in [PROJECT_SPEC §2](PROJECT_SPEC.md)).
* Pagination: cursor-based.
* Auth: bearer JWT (added in a later iteration). Tenancy is *orthogonal* to authentication — both must succeed.
* Idempotency: `POST /api/v1/carts/{id}/checkout` requires an `Idempotency-Key` header.

See [`PROJECT_SPEC.md` §5.4](PROJECT_SPEC.md) for the full API design contract.

---

## Contributing

1. Read [`PROJECT_SPEC.md`](PROJECT_SPEC.md). The spec is the contract.
2. Open a PR that references the spec section it implements.
3. Keep diffs small. One logical change per PR.
4. Tests are required for every service-layer function. Tenant-isolation tests are required for every new endpoint.
5. Inline `# ADR-NOTE:` comments for any non-obvious decision so the next engineer does not have to reverse-engineer the choice.

---

## License

TBD.
