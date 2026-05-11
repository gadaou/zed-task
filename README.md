# cart_system

Multi-tenant cart and checkout service. One PostgreSQL database, one Redis cluster, and one Celery deployment serve many tenants. Cart reads stay available when payment or invoice workers degrade, and checkout correctness is enforced under concurrent requests.

The architectural contract lives in [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

---

## Reviewer Quickstart

```bash
# 0. Copy the example env file (review DATABASE_URL / REDIS_URL if needed)
cp .env.example .env

# 1. Start all services (web, worker, db, redis) and run migrations automatically
docker compose up --build

# 2. Seed demo tenant, products, coupons, address, payment method, and cart
docker compose exec web python manage.py seed_demo_data

# 3. Open Swagger UI
open http://localhost:8000/api/docs/

# 4. Run the checkout curl command printed by seed_demo_data

# 5. Run the full test suite
docker compose exec web pytest -q
```

`make` shortcuts are available for each step: `make up` / `make seed` / `make swagger` / `make test`. See [Setup and Commands](#7-setup-and-commands) for the full reference.

## How to Review This Project in 5 Minutes

```bash
cp .env.example .env
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo_data
open http://localhost:8000/api/docs/
docker compose exec web pytest -q
```

`seed_demo_data` prints ready-to-use curl examples for checking the cart and checkout flow immediately.

Read next:
- [RUNBOOK.md](RUNBOOK.md)
- [FINAL_REVIEW.md](FINAL_REVIEW.md)
- [docs/final-verification.md](docs/final-verification.md)
- [docs/diagrams/](docs/diagrams/)

---

## Architecture at a Glance

Requests enter through thin DRF views, are dispatched to a service layer that owns all business logic, and are persisted to PostgreSQL as the sole system of record. Redis handles distributed locks, in-progress idempotency sentinels, and Celery task brokering. Celery workers process payments, invoices, and notifications asynchronously — and are only enqueued after a successful database commit.

| Diagram | What it shows |
|---|---|
| [System Architecture](docs/diagrams/system-architecture.md) | Full component map: clients, API, PostgreSQL, Redis, Celery, gateway registry |
| [Checkout Sequence](docs/diagrams/checkout-sequence.md) | 17-step checkout: idempotency, lock, `transaction.atomic`, stock, `on_commit` |
| [Data Model ERD](docs/diagrams/data-model-erd.md) | All 13 persistent models and every FK / association across 8 apps |

Full diagram index (tenant flow, payment FSM, invoice, cache, B2B): [`docs/diagrams/`](docs/diagrams/).

---

## Architecture Decisions and Trade-offs

The load-bearing choices behind the codebase, surfaced up front so reviewers do not have to reverse-engineer them.

- **Shared PostgreSQL with `tenant_id`.** All tenants live in one schema. Every model inherits `TenantAwareModel`, every composite index leads with `tenant_id`, and isolation is enforced in three independent layers (middleware, ORM manager, schema indexes). This is operationally cheap — one migration, one connection pool, one backup target — and sharding-ready: Citus can distribute by `tenant_id` without application changes. Accepted cost: a primary-DB outage is global, mitigated by HA replicas rather than per-tenant databases.
- **PostgreSQL as the sole source of truth.** No durable business state lives in Redis. Carts, orders, payments, idempotency records, and invoice numbering all persist in Postgres. A Redis flush does not lose durable business data; at worst it affects ephemeral coordination state such as cache entries, rate-limit counters, locks, or in-flight idempotency sentinels. Durable replay across Redis restarts is provided by the Postgres `IdempotencyRecord` table.
- **Redis for coordination, not storage.** Five distinct ephemeral roles, every key namespaced by `tenant_id`:
  - Distributed checkout lock — `SET NX PX` with Lua-fenced compare-token-then-`DEL` release.
  - Idempotency in-progress sentinel — fast detection ahead of the durable Postgres record.
  - Cart read-through cache — stable key `cart:read:{tenant}:{user}`, `DEL`-invalidated post-commit via [`schedule_cart_cache_invalidation`](apps/core/cache.py), TTL safety net (default 60s).
  - Per-`(tenant, user, action)` rate limiting — fixed-window `INCR + EXPIRE` in [`apps/core/throttling.py`](apps/core/throttling.py); one tenant cannot starve another's allowance.
  - Celery broker and result backend — three named queues: `payments`, `invoices`, `notifications`.
- **Celery for async payment and invoice workflows.** Payment authorization and PDF invoice generation run off the request path. Every dispatch goes through `transaction.on_commit`, so a rolled-back transaction never enqueues a payment task and an unpaid order never enqueues an invoice. Workers run with `acks_late=True` and prefetch 1; every task is written idempotently (status-guarded UPDATEs, `OneToOneField(Order)` on `Invoice`) so Celery re-deliveries are safe no-ops.
- **Dummy payment gateways instead of real provider integrations.** The `PaymentGateway` ABC plus `dummy_success` / `dummy_failing` / `dummy_timeout` prove the contract deterministically without coupling the codebase to provider-specific concerns (3DS flows, webhook verification, partial-capture quirks). Adding a real gateway is three steps — subclass the ABC, register the slug in `AppConfig.ready()`, run the shared contract test base — with zero changes to the service or task layers. See [`docs/payment-gateways.md`](docs/payment-gateways.md).
- **`X-User-Id` as an interim identity contract.** No `auth.User`, no JWT verification, no session cookies in this service. The API gateway is assumed to validate the customer token and inject `X-User-Id` (UUID) on every cart/checkout request; [`apps/core/middleware.py`](apps/core/middleware.py) binds it into a `ContextVar` so log records carry it automatically. The cart aggregate stores `user_id` as a bare `UUIDField` rather than a foreign key, keeping the bounded context independent of any specific identity provider — real auth becomes a middleware swap, not a model migration.
- **Checkout prioritizes consistency over aggressive caching.** Cart reads use short-lived read-through caching with explicit invalidation (stable key, post-commit `DEL`, TTL safety net). Checkout itself bypasses the cache: every row is re-read under `SELECT FOR UPDATE`, coupons + stock + totals are revalidated against live Postgres data, and stock deduction uses conditional `UPDATE … WHERE stock >= qty`. Caching the checkout payload would trade milliseconds of latency for a class of correctness bugs — overselling, stale coupon reuse, lost-update on totals — that this service deliberately rejects.

---

## 1. Overview

This service is multi-tenant by construction: every model carries `tenant_id`, and tenant scope is enforced consistently in middleware, ORM scoping, lock-key namespaces, and index design.

All tenants share a single PostgreSQL cluster in a shared-schema model. PostgreSQL is the only system of record, while Redis is used for coordination concerns such as locks, cache invalidation, idempotency sentinels, and rate limiting.

Checkout correctness comes from layered controls that work together: distributed locks, `transaction.atomic`, idempotency records, and conditional stock updates. Cart reads can still succeed when async payment or invoice pipelines are degraded.

---

## 2. Core Flows (Summary)

`get_or_create_active_cart` is race-safe through a partial unique index `WHERE status = 'ACTIVE'`, so the database remains the final arbiter. Cart mutations (`add_product_to_cart` / `remove_product_from_cart`) execute inside `transaction.atomic()` with `select_for_update()`, bump `Cart.version` for invalidation semantics, and rely on coupon validation at apply-time with revalidation at checkout-time.

Checkout follows a 17-step flow: idempotency replay, Redis sentinel, distributed lock, `transaction.atomic`, coupon revalidation, conditional stock deduction, order creation, `IdempotencyRecord` write, `on_commit(enqueue_authorize_payment)`, commit, and lock release. Payment authorization then runs in Celery by resolving the gateway slug and applying status-guarded FSM updates; successful authorization triggers `enqueue_generate_invoice` via `on_commit`. Invoice generation stays two-phase: persist the `Invoice` row inside `transaction.atomic`, then render the PDF outside the transaction, using `pdf_url = ""` as the retry signal after partial failure.

Full step-by-step flows with sequence diagrams: [`docs/architecture.md`](docs/architecture.md).

---

## 3. Reliability Guarantees

- **Tenant isolation.** `TenantMiddleware` resolves the tenant from `X-Tenant-Domain` and stores it in a `ContextVar`. `TenantAwareManager` is the default ORM manager on every model — all queries are automatically scoped. `tenant_id` leads every composite index.
- **Idempotent checkout.** Every checkout request must include an `Idempotency-Key` HTTP header (a client-generated UUID). The server enforces three replay rules:
  - **Same key + same body** → returns the stored response verbatim without re-executing side-effects.
  - **Same key + different body** → `409 idempotency/conflict`.
  - **Same key while the original request is still processing** → `409 idempotency/in-progress` (back off and retry with the same key).

  Durable idempotency records live in PostgreSQL (`IdempotencyRecord` table, unique on `(tenant_id, key)`). Redis is used only for in-progress coordination — a `SET NX EX` sentinel detects concurrent duplicates ahead of the durable Postgres check. A Redis flush loses no completed replay data; at worst a brief window of duplicate in-progress detection is lost until the sentinel TTL would have expired anyway.
- **Distributed lock.** Checkout acquires a Redis lock with `SET NX PX`. Release uses a Lua-fenced script that only deletes the key if the token matches — preventing a slow checkout from releasing a lock it no longer owns.
- **No overselling.** Stock deduction uses a conditional update: `UPDATE … SET stock = stock - qty WHERE stock >= qty`. Zero rows affected raises a 409 before the transaction commits. No application-level read-then-write race is possible.
- **`transaction.on_commit` discipline.** Every Celery dispatch (`enqueue_authorize_payment`, `enqueue_generate_invoice`) is registered with `on_commit`. A transaction that rolls back never orphans a payment or invoice task.
- **Cache invalidation.** `schedule_cart_cache_invalidation` is called on every cart mutation — add/remove product, coupon changes, address and payment method updates, checkout. Stale entries are never served after a write.
- **Async retry handling.** Workers use `acks_late` and prefetch 1 so tasks are not lost on worker crash. Every task is written to be idempotent — Celery re-deliveries are safe no-ops. Gateway timeouts retry with exponential backoff and jitter.

### Why idempotency matters

Idempotency is critical for payment and checkout APIs because clients may retry after timeouts. The server must avoid duplicate orders, duplicate stock deduction, and duplicate payment attempts. By storing the first successful response and replaying it on subsequent requests with the same key, the system guarantees exactly-once checkout semantics even when the network is unreliable.

---

## 4. Future Scale Path

The architectural rationale lives in [Architecture Decisions and Trade-offs](#architecture-decisions-and-trade-offs) above; this section captures only the forward-looking levers that are deliberately deferred today.

**Redis as Celery broker vs RabbitMQ.** Redis was chosen to keep the deployment to a single additional service. The payment domain logic is fully decoupled from transport: swapping to RabbitMQ is a `CELERY_BROKER_URL` change with no service-code edits. Redis-as-broker is appropriate at this scale; RabbitMQ's per-queue durability guarantees and dead-letter routing become worthwhile at higher throughput.

**Read replicas and sharding are deferred.** The principle is "no premature distribution". The database is debuggable by one engineer today. When vertical scaling is exhausted, `tenant_id`-leading indexes and the explicitly-named `all_tenants()` manager escape hatch are already in place for routing reads to replicas or migrating to Citus without schema changes.

---

## 5. Pluggable Payments

The payment system is built around a `PaymentGateway` abstract base class with two surfaces:

```python
gateway.charge(amount, currency, payment_method_data) -> ChargeResult   # simple path
gateway.authorize(...) / capture(...) / void(...) / refund(...)          # full lifecycle
```

Gateways are registered by slug and resolved at runtime — no `if/else` in service code:

```python
register_payment_gateway("stripe", StripeGateway)
gateway = get_payment_gateway("stripe")
```

Three deterministic test gateways are included: `dummy_success`, `dummy_failing`, `dummy_timeout`. To add a real gateway: subclass `PaymentGateway`, register the slug in `AppConfig.ready()`, add contract tests against the shared gateway test base, and set the slug on the tenant's payment method configuration.

Full interface contract and extension guide: [`docs/payment-gateways.md`](docs/payment-gateways.md).

---

## 6. API Reference

The OpenAPI schema is generated dynamically by drf-spectacular at `/api/schema/`. Swagger UI is available at `/api/docs/`. No static OpenAPI YAML file is committed because the generated schema is the source of truth.

| | URL |
|---|---|
| Swagger UI | `http://localhost:8000/api/docs/` |
| OpenAPI schema | `http://localhost:8000/api/schema/` |
| ReDoc | `http://localhost:8000/api/redoc/` |

### Required headers

| Header | Required on | Notes |
|---|---|---|
| `X-Tenant-Domain` | All tenant-scoped endpoints | Missing or unknown domain returns 400 |
| `X-User-Id` | All cart / checkout endpoints | UUID string; injected by the API gateway after token validation |
| `Idempotency-Key` | Checkout only | Any unique string; UUID recommended |

### Endpoints

```
# Cart — canonical RESTful endpoints (preferred, PROJECT_SPEC §5.4)
GET    /api/v1/cart/
POST   /api/v1/cart/items/
DELETE /api/v1/cart/items/{product_id}/
POST   /api/v1/cart/coupons/
DELETE /api/v1/cart/coupons/{coupon_id}/
PUT    /api/v1/cart/address/
PUT    /api/v1/cart/payment-method/
PUT    /api/v1/cart/business-details/     # B2B: company_name, tax_number, purchase_order_reference
POST   /api/v1/cart/checkout/

# Cart — legacy action-style endpoints (kept for backwards compatibility)
POST /api/v1/cart/add-product/
POST /api/v1/cart/remove-product/
POST /api/v1/cart/add-coupon/
POST /api/v1/cart/remove-coupon/
POST /api/v1/cart/add-address/
POST /api/v1/cart/add-payment-method/
POST /api/v1/cart/set-business-details/

# Explicit checkout (ops / integration tests)
POST /api/v1/carts/{cart_id}/checkout/

# Health
GET /health/    # liveness
GET /ready/     # readiness — 503 if Postgres or Redis unreachable
```

---

## 7. Setup and Commands

### Docker Compose (recommended)

```bash
cp .env.example .env      # review DATABASE_URL / REDIS_URL if needed
docker compose up --build # builds images, starts all services, runs migrations
docker compose exec web python manage.py seed_demo_data   # load demo data
```

### Makefile shortcuts

| Command | Description |
|---|---|
| `make up` | Build images and start all services |
| `make down` / `make restart` | Stop / cycle all services |
| `make seed` | Idempotent seed — tenant, products, coupons, cart |
| `make test` | Full pytest suite inside the container |
| `make test args='-k checkout'` | Subset of tests by keyword |
| `make lint` | Run `ruff` (report only) |
| `make swagger` | Open Swagger UI in the default browser |
| `make reset` | Destroy volumes and rebuild clean |
| `make logs s=worker` | Tail logs for a specific service |

### Local (without Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + test deps and pinned Ruff (lint/format)
cp .env.example .env                       # set DATABASE_URL and REDIS_URL
python manage.py migrate
python manage.py seed_demo_data
DJANGO_SETTINGS_MODULE=cart_system.settings.test pytest -q
```

If your local `.env` points `DATABASE_URL` to PostgreSQL, either start Postgres through Docker Compose or override `DATABASE_URL=sqlite:///:memory:` for the fast local test suite.

### Settings modules

| Module | Used for |
|---|---|
| `cart_system.settings.dev` | Local development — `DEBUG=True`, browsable API |
| `cart_system.settings.prod` | Production — HSTS, secure cookies, JSON logging |
| `cart_system.settings.test` | Test runs — SQLite fallback, `CELERY_TASK_ALWAYS_EAGER` |

---

## 8. Observability

Every request carries an `X-Request-Id` (preserved if sent by the client, otherwise generated). The ID is bound into a `ContextVar` so every log record emitted during the request — in middleware, service layer, signal handlers — automatically carries `request_id` without explicit argument passing.

Structured logs are human-readable in development and JSON in production. `apps.core.metrics.incr(name, **labels)` emits metric events as structured log records — replace `incr()` with a real counter client in production without changing any call site.

Key metrics: `checkout.failed`, `checkout.lock_contention`, `payment.authorized`, `payment.declined`, `payment.timeout`, `idempotency.replay`, `idempotency.conflict`, `invoice.failed`, `readiness.dependency_failed`.

Full details: [`docs/observability.md`](docs/observability.md).

---

## Repository Layout

```text
.
├── PROJECT_SPEC.md            ← architectural contract — read this first
├── README.md
├── Makefile
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── manage.py
├── requirements.txt
├── requirements-dev.txt
├── cart_system/
│   ├── settings/              ← base / dev / prod / test
│   ├── urls.py
│   └── celery.py
├── apps/
│   ├── core/                  ← health, request-id, idempotency, metrics, seed command
│   ├── tenant/                ← Tenant model, TenantMiddleware, TenantAwareManager
│   ├── catalog/               ← Product — price, stock, currency
│   ├── cart/                  ← Cart, CartItem, add/remove/recalculate
│   ├── coupon/                ← Coupon, CartCoupon, rule registry, stacking
│   ├── addresses/             ← Address — soft-delete, one default per user
│   ├── payment/               ← Payment, PaymentMethod, gateway registry
│   ├── order/                 ← Order, OrderItem, CheckoutService
│   └── invoice/               ← Invoice, InvoiceSequence, two-phase PDF generation
└── docs/
    ├── architecture.md        ← full design, all flows, concurrency, design principles
    ├── observability.md       ← structured logging, metrics, request correlation
    ├── payment-gateways.md    ← gateway interface, registration, adding real gateways
    ├── test-quality-summary.md← test counts, coverage, quality notes
    └── diagrams/
        ├── system-architecture.md
        ├── checkout-sequence.md
        ├── data-model-erd.md
        ├── tenant-isolation-flow.md
        ├── payment-flow.md
        ├── invoice-flow.md
        ├── cache-idempotency-locks.md
        └── b2b-flow.md
```

Each app follows the same internal layout: `models.py` → `services.py` → `views.py` → `serializers.py` → `urls.py` → `tests/`.

---

## Documentation Index

| Document | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Full system design, all core flows, concurrency model, design principles |
| [`docs/observability.md`](docs/observability.md) | Structured logging, metric hooks, request correlation, JSON production format |
| [`docs/payment-gateways.md`](docs/payment-gateways.md) | Gateway interface contract, registration pattern, adding real gateways, testing |
| [`docs/test-quality-summary.md`](docs/test-quality-summary.md) | Test counts by category, feature coverage, concurrency and idempotency test notes |
| [`docs/diagrams/`](docs/diagrams/) | 8 Mermaid diagrams: system architecture, checkout sequence, ERD, tenant isolation, payment FSM, invoice generation, cache/locks, B2B flow |

---

## Architecture Flowcharts

These diagrams summarize the main runtime flows, data boundaries, and reliability mechanisms used by the platform.

| Diagram | What it explains | Why it matters |
|---|---|---|
| [System Architecture](docs/diagrams/system-architecture.md) | How clients, DRF, the service layer, PostgreSQL, Redis, Celery, and payment and invoice paths fit together. | Gives reviewers a single map of components before diving into code or deeper diagrams. |
| [Data Model ERD](docs/diagrams/data-model-erd.md) | Tenant-owned models and how cart, order, payment, and invoice entities relate, including key constraints. | Clarifies persistence boundaries and FK relationships across apps in one view. |
| [Checkout Sequence](docs/diagrams/checkout-sequence.md) | Idempotency checks, Redis lock acquisition, the DB transaction, stock updates, and async work dispatched after commit. | Shows where correctness and concurrency guarantees are enforced end-to-end. |
| [Tenant Isolation](docs/diagrams/tenant-isolation-flow.md) | Resolution from `X-Tenant-Domain` through middleware and `ContextVar` to `TenantAwareManager` and 404-style isolation. | Makes the multi-tenant enforcement path explicit for security and data-leak reviews. |
| [Payment Flow](docs/diagrams/payment-flow.md) | Gateway registry, dummy gateways for tests, payment state transitions, and retry behavior. | Documents how money-moving logic stays pluggable and safe under worker retries. |
| [Invoice Flow](docs/diagrams/invoice-flow.md) | Two-phase invoice creation, per-tenant numbering, and retry-safe PDF generation. | Explains durable billing artifacts and crash recovery around PDF creation. |
| [Cache / Idempotency / Locks](docs/diagrams/cache-idempotency-locks.md) | Cart cache invalidation, durable idempotency records, and the Lua-fenced Redis checkout lock. | Ties together caching, replay semantics, and lock safety without reading three separate areas first. |
| [B2B Flow](docs/diagrams/b2b-flow.md) | Business fields on the cart, how they snapshot onto the order, and how invoices consume that data. | Helps reviewers trace B2B-specific data from API through fulfillment documents. |

GitHub renders Mermaid diagrams directly from these markdown files.

---

## License

TBD.
