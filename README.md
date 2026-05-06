# cart_system

Multi-tenant cart and checkout service built for a single-deployment SaaS commerce model: one codebase, one PostgreSQL cluster, one Redis cluster, many tenants.

The system contract lives in [`PROJECT_SPEC.md`](PROJECT_SPEC.md). The implementation snapshot and flow details live in [`docs/architecture.md`](docs/architecture.md).

---

## 1) Project Overview

`cart_system` is a multi-tenant commerce backend designed to keep cart and checkout flows reliable under shared infrastructure.

- **Multi-tenant by construction**: tenancy is encoded in request handling, ORM access patterns, lock keys, and service boundaries.
- **Single database model**: all tenants share one PostgreSQL source of truth using `tenant_id` scoped data access.
- **Always-online posture**: cart reads remain available even when payment/invoice pipelines degrade; checkout paths prioritize safe partial degradation over hard downtime.

---

## 2) Architecture

Core stack:

- **Django + Django REST Framework** for API delivery and service-layer architecture.
- **PostgreSQL** as the system of record for all tenant data.
- **Redis** for distributed locks, idempotency in-progress markers, and cache/coordination concerns.
- **Celery** for async workloads such as payment authorization/finalization and invoice/notification pipelines.

High-level request flow:

`Client -> DRF View -> Service Layer -> PostgreSQL (+ Redis coordination) -> on_commit -> Celery Worker -> Gateway`

---

## 3) Core Features

Current service scope centers on cart and checkout operations:

- Add product to cart
- Remove product from cart
- Apply coupon to cart
- Remove coupon from cart
- Add address
- Add payment method
- Checkout cart

Endpoints are versioned under `/api/v1/` and follow resource-oriented conventions.

---

## 4) Reliability Features

The reliability model combines database guarantees with distributed coordination:

- **Tenant isolation** via middleware + tenant-aware ORM manager + tenant-led indexes
- **Transactional boundaries** around critical mutations using `transaction.atomic()`
- **Row-level serialization** with `select_for_update` on sensitive state transitions
- **Redis distributed locks** for cross-process checkout serialization (`SET NX PX` + fenced unlock)
- **Idempotency** for checkout using `Idempotency-Key` and durable replay records
- **Conditional stock deduction** (`UPDATE ... WHERE stock >= qty`) to prevent negative stock races
- **`transaction.on_commit` discipline** so async tasks are only dispatched after successful commit

---

## 5) Payment System

Payments use a pluggable gateway architecture behind a stable abstraction.

- Domain/services depend on a `PaymentGateway` interface, not concrete providers
- Gateways are resolved through a registry (`register_payment_gateway`, `get_payment_gateway`)
- No `if/else` provider branching in service flow

For assignment/testing, deterministic dummy gateways are included:

- `DummySuccessGateway`
- `DummyFailingGateway`
- `DummyTimeoutGateway`

To add a real gateway later:

1. Implement the `PaymentGateway` interface
2. Register it in the gateway registry
3. Add contract tests against the shared gateway contract suite
4. Configure tenant/provider mapping to point at the new slug

See [`docs/payment-gateways.md`](docs/payment-gateways.md).

---

## 6) Coupon System

Coupons are implemented with a rule-based constraint model and transactional safety:

- **Rule registry** for extensible constraint validation (open for extension)
- **Usage limits** enforced with conditional increments and DB constraints
- **Stacking policy** support (single-only / one-per-type / unlimited patterns)
- **Checkout revalidation** to catch drift between coupon apply-time and checkout-time

This preserves correctness under concurrency and evolving cart state.

---

## 7) Bonus Features

Planned and/or partially implemented bonus scope includes:

- **Invoice handling**: async invoice creation and delivery pipeline
- **Advanced coupon constraints**: subtotal, location, segment, usage, validity windows, allow/deny lists
- **B2B support**: approvals, payment terms, B2B checkout states (where implemented)

Authoritative roadmap and constraints: [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

---

## 8) API Documentation

- Swagger UI: `http://localhost:8000/api/docs/`
- OpenAPI schema: `http://localhost:8000/api/schema/`
- ReDoc: `http://localhost:8000/api/redoc/`

Required headers:

- `X-Tenant-Domain` on tenant-scoped endpoints
- `Idempotency-Key` on checkout (`POST /api/v1/carts/{cart_id}/checkout`)

Health endpoints:

- `GET /health/` (liveness)
- `GET /ready/` (readiness)
- `GET /healthz` and `GET /readyz` are compatibility aliases.

Reminder:

- `/health/` checks application liveness.
- `/ready/` checks whether required dependencies such as PostgreSQL and Redis are available.

---

## 9) How to Run

### Local (current, canonical)

```bash
# 1) Setup
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 2) Environment
cp .env.example .env
# update DATABASE_URL / REDIS_URL and related settings

# 3) Migrate
python manage.py migrate

# 4) Run API
python manage.py runserver

# 5) Run tests
DJANGO_SETTINGS_MODULE=cart_system.settings.test pytest -q
```

### Docker Compose (when compose file is present)

> The current repository snapshot does not include `docker-compose.yml`. If/when compose is added, standard workflow is:

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web pytest -q
```

---

## 10) Trade-offs

Key architectural trade-offs are intentional:

- **Shared DB + `tenant_id`**
  - Pros: operational simplicity, cheap tenant onboarding, unified migrations
  - Trade-off: larger blast radius if primary DB degrades
- **Async payment processing**
  - Pros: resilient checkout UX, retryable gateway interactions
  - Trade-off: eventual consistency on final payment state
- **Redis + PostgreSQL hybrid idempotency**
  - Pros: fast in-progress guard + durable replay guarantee
  - Trade-off: dual-store operational complexity
- **No real external gateway integration yet**
  - Pros: deterministic tests, clean boundary-first architecture
  - Trade-off: provider-specific behavior deferred to later iterations

---

## Repository Layout

```text
.
├── PROJECT_SPEC.md
├── README.md
├── manage.py
├── requirements.txt
├── cart_system/
│   ├── settings/
│   ├── urls.py
│   └── celery.py
├── apps/
│   ├── core/
│   ├── tenant/
│   ├── catalog/
│   ├── cart/
│   ├── coupon/
│   ├── addresses/
│   ├── payment/
│   └── order/
└── docs/
    ├── architecture.md
    ├── payment-gateways.md
    └── openapi.yaml
```

---

## Contributing

1. Read [`PROJECT_SPEC.md`](PROJECT_SPEC.md) first.
2. Reference the exact spec section your change implements.
3. Keep diffs focused and reversible.
4. Add tests for all service-layer behavior changes.
5. Document non-obvious decisions with concise ADR notes close to code.

---

## License

TBD.

---

## Legacy README (Preserved)

Below is the original README content, kept intact per your request.

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
* Payment gateways: pluggable behind the `PaymentGateway` ABC and a slug-keyed registry — see [`docs/payment-gateways.md`](docs/payment-gateways.md).

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
