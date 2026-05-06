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
- **Celery** for async workloads such as payment authorization/finalization and invoice/notification pipelines. Redis is used as the Celery broker for this assessment to keep the deployment simple and aligned with the existing Redis dependency used for locks and idempotency. For higher-scale production workloads, the broker can be replaced with RabbitMQ or another dedicated messaging system without changing the payment domain logic.

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

## 4) Reliability & Consistency

The reliability model combines database guarantees with distributed coordination:

- **Tenant isolation** via middleware + tenant-aware ORM manager + tenant-led indexes
- **Transactional boundaries** around critical mutations using `transaction.atomic()`
- **Row-level serialization** with `select_for_update` on sensitive state transitions
- **Redis distributed locks** for cross-process checkout serialization (`SET NX PX` + fenced Lua unlock)
- **Idempotency** for checkout using `Idempotency-Key` and durable `IdempotencyRecord` replay
- **Conditional stock deduction** (`UPDATE ... WHERE stock >= qty`) to prevent negative-stock races
- **`transaction.on_commit` discipline** — Celery tasks are only enqueued after a successful `COMMIT`; a rolled-back checkout never orphans a payment or invoice task
- **Retryable async tasks** — payment and invoice workers are designed with `max_retries`, `retry_backoff`, `retry_jitter`, and `acks_late`; each task is idempotent so Celery re-deliveries are safe no-ops
- **Status-guarded UPDATEs** — FSM transitions on `Payment.status`, `Order.status`, and `Invoice.pdf_url` are written as `UPDATE … WHERE status = <expected>`, turning zero rows affected into a safe idempotency signal rather than a hidden bug

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

## 7) Invoice System

Invoices are generated asynchronously after a payment is confirmed, using a two-phase approach designed to keep database transactions short and avoid mixing file I/O with database locks.

### Phase 1 — Transactional DB work (inside `transaction.atomic`)

1. Lock the `InvoiceSequence` row for the tenant with `select_for_update`.
2. Atomically increment the per-tenant monotonic invoice number (`last_number + 1`).
3. `INSERT` an `Invoice` row with `pdf_url = ""` (committed immediately; the row exists before any I/O begins).

### Phase 2 — PDF rendering (outside the transaction)

4. Call `render_invoice_pdf(...)` using ReportLab to write the PDF to `MEDIA_ROOT/invoices/<id>.pdf`.
5. Apply a **status-guarded UPDATE**: `UPDATE invoice SET pdf_url = <url> WHERE pk = <id> AND pdf_url = ""`; zero rows affected means another worker already set the URL — safe no-op.

**Why two phases?**

| Concern | Resolution |
|---|---|
| Long DB locks | PDF I/O is never inside the transaction; lock hold time is microseconds |
| File I/O inside transactions | Failure at step 4 never leaves an open transaction or partial DB state |
| Retryable PDF generation | Phase 1 is idempotent via `OneToOneField` constraint; phase 2 is idempotent via the status-guarded UPDATE |
| Crash recovery | A row with `pdf_url = ""` is the durable signal for a pending retry; the worker re-renders without re-allocating a sequence number |

The `generate_invoice` Celery task runs on the `invoices` queue and is dispatched via `transaction.on_commit` after `Order.status` transitions to `PAID`.

Authoritative roadmap: [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

---

## 8) Scaling Considerations

The architecture is designed to scale horizontally without requiring a distributed rewrite.

**Today (single-process baseline):**
- One Django process handles all API traffic; Gunicorn workers add concurrency without coordination overhead.
- Redis acts as the shared coordination layer for distributed locks and idempotency — adding API servers requires no changes here, the lock key namespace (`lock:checkout:{tenant_id}:{cart_id}`) already distributes cleanly.
- PostgreSQL is the single source of truth; composite indexes lead with `tenant_id` so tenant-scoped queries hit narrow B-tree ranges.

**Near-term (read scaling):**
- Non-mutating paths (product browse, cart reads, coupon lookup) can route to a Postgres read replica with a one-line Django `DATABASES` routing change — no ORM or service changes required.
- Redis client-side caching can front hot catalog reads (product price, stock availability) for tenants with high read-to-write ratios.

**Longer-term (write scaling + multi-region):**
- Every table carries `tenant_id` and every query filters by it. Citus (Postgres extension) can shard by `tenant_id` column with no application-layer changes — the `all_tenants()` manager escape hatch is already the only cross-tenant query path.
- The Celery queue split (`payments`, `invoices`, `notifications`) allows scaling workers independently — payment workers are I/O-bound on gateway latency; invoice workers are CPU-bound on PDF rendering.
- The event-driven skeleton is already in place (`transaction.on_commit` → Celery); migrating to an event broker (Kafka, SQS, or RabbitMQ) is a task-dispatch replacement, not an architectural change. The payment domain logic is fully decoupled from the broker — swapping `CELERY_BROKER_URL` and the relevant transport package is the only change required.

**Explicit non-goals (for now):**
Multi-region active-active, per-tenant databases, and event sourcing are deferred per spec §7.5: "No premature distribution."

---

## 9) API Documentation

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

## 10) Demo Seed & Quick Checkout

The fastest way to see the full checkout flow in action.

### Seed demo data

```bash
python manage.py seed_demo_data
```

This creates (idempotently — safe to run multiple times):

| Object | Value |
|---|---|
| Tenant | `demo.localhost` |
| Products | Wireless Headphones ($79.99), Mechanical Keyboard ($129.99), USB-C Hub ($34.99) |
| Coupons | `DEMO10` (10% off), `SAVE5` ($5 fixed) |
| Customer UUID | `00000000-0000-0000-0000-000000000001` |
| Address | San Francisco, US — set as default |
| Payment method | `dummy_success` gateway |
| Cart | Pre-loaded with 2 items, ready for checkout |

The command prints a ready-to-paste `curl` checkout command at the end. No manual ID hunting required.

### Skip cart creation

```bash
python manage.py seed_demo_data --no-cart
```

All objects except the cart are seeded. Useful if you want to build the cart via the API yourself.

### Test checkout in under 2 minutes

After seeding, the command output includes a complete curl snippet. Copy and run it:

```bash
# 1. Seed (first time or repeat — safe either way)
python manage.py seed_demo_data

# 2. Copy the curl command printed at the end, which looks like:
curl -s -X POST http://localhost:8000/api/v1/carts/<cart-id>/checkout/ \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{
    "payment_method_id": "<payment-method-id>",
    "address_id": "<address-id>"
  }' | python3 -m json.tool

# 3. Optional: apply the DEMO10 coupon before checkout
curl -s -X POST http://localhost:8000/api/v1/carts/<cart-id>/coupons/ \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Domain: demo.localhost" \
  -d '{"coupon_code": "DEMO10"}' | python3 -m json.tool
```

Expected response: `202 Accepted` with `payment_status: "pending"` (the `dummy_success` gateway authorises synchronously in test/dev settings).

---

## 11) Makefile Commands (Docker)

All commands run against the Docker Compose stack. Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Docker Engine with the Compose v2 plugin.

### Stack lifecycle

| Command | Description |
|---|---|
| `make up` | Build images and start all services (web, worker, db, redis) |
| `make down` | Stop all services |
| `make restart` | `down` then `up` |
| `make logs` | Tail logs for all services |
| `make logs s=web` | Tail logs for a specific service (`web`, `worker`, `db`, `redis`) |
| `make build` | Rebuild images without starting |
| `make reset` | **Destroy everything** (volumes included), rebuild from scratch, start clean |

### Database

| Command | Description |
|---|---|
| `make migrate` | Apply all pending migrations |
| `make makemigrations` | Generate new migration files across all apps |
| `make makemigrations app=invoice` | Generate migrations for a specific app |

### Demo data

| Command | Description |
|---|---|
| `make seed` | Idempotent seed — tenant, products, coupons, address, payment method, cart |
| `make seed args=--no-cart` | Seed everything except the demo cart |

### Quality

| Command | Description |
|---|---|
| `make test` | Run the full pytest suite inside the container |
| `make test args='-k checkout'` | Run a subset of tests matching a keyword |
| `make lint` | Run `ruff` linter (reports only, no auto-fix) |

### Developer tools

| Command | Description |
|---|---|
| `make shell` | Open a Django shell (`python manage.py shell`) |
| `make shell-db` | Open `psql` inside the `db` container |
| `make swagger` | Open Swagger UI in the default browser |

### Zero-to-checkout in four commands

```bash
make up          # start the full stack (runs migrations automatically)
make seed        # create demo tenant, products, coupons, and a ready cart
# copy the curl command printed by seed and run it
make logs        # watch the Celery worker process the payment + invoice tasks
```

---

## 12) How to Run

### Docker Compose (recommended)

```bash
cp .env.example .env   # adjust DATABASE_URL / REDIS_URL if needed
make up                # builds images, starts all services, runs migrations
make seed              # load demo data
# API is live at http://localhost:8000
```

### Local (without Docker)

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

# 5) Seed demo data
python manage.py seed_demo_data

# 6) Run tests
DJANGO_SETTINGS_MODULE=cart_system.settings.test pytest -q
```

---

## 13) Trade-offs

Key architectural trade-offs are intentional and documented here for reviewers.

| Decision | Pros | Accepted cost |
|---|---|---|
| **Shared schema multi-tenancy (`tenant_id` column)** | Operational simplicity; one migration run; zero onboarding cost per tenant; sharding-ready by design | Larger blast radius if the primary DB degrades; row-level security (RLS) deferred |
| **Async payment processing (Celery)** | Resilient checkout UX; retryable gateway interactions; checkout never blocks on gateway latency | Eventual consistency on final payment state; requires idempotency discipline in every task |
| **Two-phase invoice generation** | Short DB lock hold time; PDF I/O never inside a transaction; clean retry semantics | Two database writes per invoice instead of one; `pdf_url = ""` sentinel must be monitored |
| **Redis + PostgreSQL hybrid idempotency** | Fast in-progress guard in Redis; durable replay guarantee in Postgres | Dual-store operational complexity; Redis TTL expiry must outlive the longest possible checkout |
| **Dummy payment gateways instead of real integrations** | Deterministic tests; clean `PaymentGateway` interface proven before real provider coupling | Provider-specific error codes, 3DS flows, and webhook verification deferred |
| **Rule registry for coupon constraints** | Open for extension; zero service-code changes per new constraint type; unknown keys fail closed | Constraints opaque to SQL — cannot `WHERE` on JSON fields efficiently without Postgres JSON path operators |
| **No premature distribution** | Dramatically lower operational surface; one process, one DB, one Redis is debuggable by one engineer | Vertical scaling ceiling; migration to multi-region or sharded topology is a future investment |

---

## 14) Repository Layout

```text
.
├── PROJECT_SPEC.md
├── README.md
├── Makefile               ← make up / test / seed / reset / …
├── Dockerfile             ← dev + prod multi-stage build
├── docker-compose.yml     ← web, worker, db, redis
├── .env.example
├── manage.py
├── requirements.txt
├── cart_system/
│   ├── settings/
│   ├── urls.py
│   └── celery.py
├── apps/
│   ├── core/          # health, idempotency, Redis lock, seed command
│   ├── tenant/        # Tenant model, middleware, TenantAwareManager
│   ├── catalog/       # Product — price, stock, currency
│   ├── cart/          # Cart, CartItem, add/remove/recalculate
│   ├── coupon/        # Coupon, CartCoupon, rule registry, stacking
│   ├── addresses/     # Address — soft-delete, one default per user
│   ├── payment/       # Payment, PaymentMethod, gateway registry
│   ├── order/         # Order, OrderItem, CheckoutService
│   └── invoice/       # Invoice, InvoiceSequence, two-phase PDF generation
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
