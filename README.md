# cart_system

Multi-tenant cart and checkout service. One PostgreSQL database, one Redis cluster, one Celery deployment — many tenants. Built reliability-first: cart reads survive payment pipeline degradation, and checkout correctness is guaranteed under concurrent requests.

The architectural contract lives in [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

---

## Reviewer Quickstart

```bash
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

## 1. Overview

- **Multi-tenant by construction.** Every model carries `tenant_id`. The ORM manager, middleware, lock-key namespace, and index design all treat tenant scope as a first-class constraint — not an afterthought.
- **Single PostgreSQL database.** All tenants share one cluster using a shared-schema model. PostgreSQL is the only system of record; Redis is coordination infrastructure, not storage.
- **Reliability-first design.** Cart reads remain available when payment or invoice pipelines degrade. Checkout uses a layered safety model: distributed locks, database transactions, idempotency records, and conditional stock updates that make concurrent races safe by construction.

---

## 2. Core Flows (Summary)

- **Active cart resolution** — `get_or_create_active_cart` is race-safe via a partial unique index `WHERE status = 'ACTIVE'`; the database is the arbiter, not the application.
- **Cart mutations** — `add_product_to_cart` / `remove_product_from_cart` run inside `transaction.atomic()` with `select_for_update()` and increment `Cart.version` for cache invalidation. Coupon rules are evaluated at apply-time and re-validated at checkout-time.
- **Checkout** — a 17-step protocol: idempotency replay → Redis sentinel → distributed lock → `transaction.atomic` → coupon revalidation → conditional stock deduction → order creation → `IdempotencyRecord` write → `on_commit(enqueue_authorize_payment)` → commit → lock release.
- **Payment** — the `authorize_payment` Celery task resolves the gateway by slug, applies status-guarded FSM transitions, and dispatches `enqueue_generate_invoice` on success via `on_commit`.
- **Invoice** — two-phase: Phase 1 commits the `Invoice` row inside `transaction.atomic`; Phase 2 renders the PDF outside the transaction. The `pdf_url = ""` sentinel is the crash-recovery signal.

Full step-by-step flows with sequence diagrams: [`docs/architecture.md`](docs/architecture.md).

---

## 3. Reliability Guarantees

- **Tenant isolation.** `TenantMiddleware` resolves the tenant from `X-Tenant-Domain` and stores it in a `ContextVar`. `TenantAwareManager` is the default ORM manager on every model — all queries are automatically scoped. `tenant_id` leads every composite index.
- **Idempotent checkout.** `Idempotency-Key` triggers a two-layer check: a Redis sentinel (`SET NX EX`) for fast in-progress detection, and a PostgreSQL `IdempotencyRecord` for durable replay. Replayed responses are bit-for-bit identical — the serialized payload is stored, not recomputed.
- **Distributed lock.** Checkout acquires a Redis lock with `SET NX PX`. Release uses a Lua-fenced script that only deletes the key if the token matches — preventing a slow checkout from releasing a lock it no longer owns.
- **No overselling.** Stock deduction uses a conditional update: `UPDATE … SET stock = stock - qty WHERE stock >= qty`. Zero rows affected raises a 409 before the transaction commits. No application-level read-then-write race is possible.
- **`transaction.on_commit` discipline.** Every Celery dispatch (`enqueue_authorize_payment`, `enqueue_generate_invoice`) is registered with `on_commit`. A transaction that rolls back never orphans a payment or invoice task.
- **Cache invalidation.** `schedule_cart_cache_invalidation` is called on every cart mutation — add/remove product, coupon changes, address and payment method updates, checkout. Stale entries are never served after a write.
- **Async retry handling.** Workers use `acks_late` and prefetch 1 so tasks are not lost on worker crash. Every task is written to be idempotent — Celery re-deliveries are safe no-ops. Gateway timeouts retry with exponential backoff and jitter.

---

## 4. Trade-offs and Future Scale Path

**Shared schema vs separate databases.** All tenants share one PostgreSQL cluster (shared-schema model). This keeps operational overhead minimal — one migration run, zero per-tenant onboarding cost — and the schema is already sharding-ready because `tenant_id` leads every composite index. The accepted cost is a larger blast radius if the primary DB degrades; per-tenant DB isolation is a future migration path, not a redesign.

**Redis as Celery broker vs RabbitMQ.** Redis was chosen to keep the deployment to a single additional service. The payment domain logic is fully decoupled from transport: swapping to RabbitMQ requires only a `CELERY_BROKER_URL` change. Redis-as-broker is appropriate at this scale; RabbitMQ's per-queue durability guarantees and dead-letter routing become worthwhile at higher throughput.

**PostgreSQL as the sole system of record.** Redis holds no durable state — locks and idempotency sentinels are ephemeral by design. If Redis flushes, in-flight checkouts may see `409 lock-contention` until TTLs expire, but no data is lost. PostgreSQL `IdempotencyRecord` rows provide durable replay even across Redis restarts.

**Read replicas and sharding are deferred.** The principle is "no premature distribution". The database is debuggable by one engineer. When vertical scaling is exhausted, `tenant_id`-leading indexes and the `all_tenants()` manager escape hatch are already in place for routing reads to replicas or migrating to Citus without schema changes.

**No real payment gateway.** The clean `PaymentGateway` interface is proven against deterministic dummies (`dummy_success`, `dummy_failing`, `dummy_timeout`) before any provider coupling is introduced. Provider-specific concerns — 3DS flows, webhook verification, error-code mapping — are explicitly out of scope for this assessment. Adding a real gateway requires only subclassing `PaymentGateway` and registering the slug. See [`docs/payment-gateways.md`](docs/payment-gateways.md).

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
# Cart (customer-facing — active cart resolved automatically)
GET  /api/v1/cart/
POST /api/v1/cart/add-product/
POST /api/v1/cart/remove-product/
POST /api/v1/cart/add-coupon/
POST /api/v1/cart/remove-coupon/
POST /api/v1/cart/add-address/
POST /api/v1/cart/add-payment-method/
POST /api/v1/cart/set-business-details/   # B2B: company_name, tax_number, purchase_order_reference
POST /api/v1/cart/checkout/

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
