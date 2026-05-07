# cart_system

Multi-tenant cart and checkout service. One PostgreSQL database, one Redis cluster, one Celery deployment — many tenants. The system is built reliability-first: cart reads survive payment pipeline degradation, and checkout correctness is guaranteed under concurrent requests.

The architectural contract lives in [`PROJECT_SPEC.md`](PROJECT_SPEC.md). Implementation details and flow diagrams live in [`docs/architecture.md`](docs/architecture.md).

---

## Reviewer Quickstart

```bash
make up           # build images, start web / worker / db / redis, run migrations
make seed         # create demo tenant, products, coupons, address, payment method, cart
make swagger      # open http://localhost:8000/api/docs/ in the browser
# copy the curl checkout command printed by `make seed` and run it
make test         # full pytest suite
make logs s=worker  # watch Celery process the payment + invoice tasks
```

That is the complete end-to-end loop. Everything below explains what is happening under the hood.

---

## 1. Overview

- **Multi-tenant by construction.** Every model carries `tenant_id`. The ORM manager, middleware, lock-key namespace, and index design all treat tenant scope as a first-class constraint — not an afterthought.
- **Single PostgreSQL database.** All tenants share one cluster using a shared-schema model. PostgreSQL is the only system of record; Redis is coordination infrastructure, not storage.
- **Reliability-first design.** Cart reads remain available when payment or invoice pipelines degrade. Checkout uses a layered safety model: distributed locks, database transactions, idempotency records, and conditional stock updates that make concurrent races safe by construction.

---

## 2. Architecture

```
Client
  └── DRF View (thin: parse, validate, dispatch, serialize)
        └── Service Layer (transactions, locks, domain logic)
              ├── PostgreSQL  ← source of truth for all state
              └── Redis       ← locks, idempotency sentinels, Celery broker
                    ↓ transaction.on_commit
              Celery Worker
                    └── Payment Gateway / PDF renderer
```

**Stack:**

- **Django REST Framework** — versioned API under `/api/v1/`, cursor pagination, `drf-spectacular` for OpenAPI 3 docs. Views are intentionally thin; all business logic lives in `services.py`.
- **PostgreSQL** — the only durable store. Every table has `tenant_id` as the leading index column. Cross-tenant queries go through a single `all_tenants()` manager escape hatch (used only by async workers that need to resolve an order back to its tenant).
- **Redis** — three roles: distributed checkout locks (`SET NX PX` + fenced Lua unlock), in-progress idempotency sentinels (`SET NX EX`), and Celery broker. Redis is used as the broker for this assessment to keep the deployment to a single additional service; the payment domain logic is fully decoupled from transport, so swapping to RabbitMQ is a `CELERY_BROKER_URL` change.
- **Celery** — three named queues: `payments`, `invoices`, `notifications`. Workers use `acks_late` and prefetch 1 so tasks are not lost on worker crash. Every task is written to be idempotent — Celery re-deliveries are safe no-ops.

---

## 3. Core Flows

### 3.1 Active cart resolution

`get_or_create_active_cart(tenant, user_id)` does a GET then INSERT. A partial unique index on `(tenant_id, user_id) WHERE status = 'ACTIVE'` makes the race safe: if two concurrent requests both pass the GET phase, only one INSERT wins; the loser retries with a GET and returns the winner's row. CHECKED_OUT carts are excluded from the index so historical orders never block a new purchase.

### 3.2 Add / remove product

`add_product_to_cart` and `remove_product_from_cart` run inside `transaction.atomic()` with `select_for_update()` on the cart row. Every mutation increments `Cart.version` using `F("version") + 1` — a cheap optimistic-concurrency signal for cache invalidation. `recalculate_cart()` recomputes totals and applied coupon discounts before the transaction commits.

### 3.3 Coupon apply / remove

Applying a coupon runs the coupon through the rule registry (usage limits, stacking policy, validity window) before writing a `CartCoupon` record. Checkout revalidates all applied coupons at transaction time to catch drift between apply-time and checkout-time state — a coupon that was valid when applied can still be rejected at checkout if usage limits were reached concurrently.

### 3.4 Address and payment method selection

`set_cart_address` associates a validated address with the cart. `add_payment_method(gateway_slug=...)` resolves the slug through the gateway registry before persisting the `PaymentMethod` — an unsupported gateway slug is rejected at creation time, not at checkout.

### 3.5 Checkout

`CheckoutService.checkout()` runs a 17-step protocol:

1. Check for an existing `IdempotencyRecord` — if found, replay the stored response immediately.
2. Check the Redis in-progress sentinel — if found, return `409 in-progress` to the concurrent caller.
3. Acquire the Redis checkout lock (`SET NX PX`). If the lock is held, return `409 lock-contention`.
4. Open `transaction.atomic()`.
5. Lock the cart row with `select_for_update()`.
6. Revalidate all applied coupons.
7. Deduct stock: `UPDATE catalog_product SET stock = stock - qty WHERE pk = ? AND stock >= qty`. Zero rows updated raises a 409 — no overselling is possible.
8. Create `Order` and `OrderItem` rows.
9. Transition the cart to `CHECKED_OUT`.
10. Create a `Payment` record in `REQUIRES_CONFIRMATION` state.
11. Write the `IdempotencyRecord` with the serialized response payload.
12. Register `transaction.on_commit(enqueue_authorize_payment)`.
13. Commit.
14. In the `finally` block: clear the Redis lock and the in-progress sentinel.

The `on_commit` hook guarantees the Celery task is enqueued only after a successful commit. A rolled-back checkout never orphans a payment or invoice task.

### 3.6 Payment authorization

The `authorize_payment` Celery task (queue: `payments`) resolves the payment gateway by slug, calls `gateway.authorize(...)`, and applies status-guarded updates:

- `Payment.status`: `REQUIRES_CONFIRMATION → AUTHORIZED` (or `FAILED` on decline).
- `Order.status`: `PENDING_PAYMENT → PAID`.

The UPDATE uses `WHERE status = <expected>` — zero rows affected is treated as a safe idempotency signal, not an error. On success, `transaction.on_commit(enqueue_generate_invoice)` dispatches the invoice task. Gateway timeouts increment the `payment.timeout` metric and re-raise so Celery retries with backoff and jitter.

### 3.7 Invoice generation

Two phases, intentionally separated to keep the database transaction short and to avoid mixing file I/O with database locks.

**Phase 1 — inside `transaction.atomic()`:**
Lock the `InvoiceSequence` row for the tenant with `select_for_update()`, atomically increment `last_number`, and INSERT an `Invoice` row with `pdf_url = ""`. The row is committed immediately; the invoice exists in the database before any file I/O begins.

**Phase 2 — outside the transaction:**
Call `render_invoice_pdf(...)` (ReportLab) to write the file to `MEDIA_ROOT/invoices/<id>.pdf`. Apply a status-guarded update: `UPDATE invoice SET pdf_url = <url> WHERE pk = ? AND pdf_url = ""`. Zero rows affected means another worker already completed the PDF — safe no-op.

A row with `pdf_url = ""` is the durable retry signal. If the worker crashes between Phase 1 and Phase 2, the next delivery re-renders the PDF without re-allocating a sequence number, because Phase 1 is protected by the `OneToOneField` constraint on `Order`.

---

## 4. Key Guarantees

- **Tenant isolation.** `TenantMiddleware` resolves the tenant from `X-Tenant-Domain` and stores it on the request. `TenantAwareManager` is the default ORM manager on every model — it automatically scopes all queries to the current tenant. `tenant_id` leads every composite index.

- **One active cart per tenant/user.** Enforced at the database level by a partial unique index `WHERE status = 'ACTIVE'`. Application-level retries handle the race; the database is the final arbiter.

- **Idempotent checkout.** `Idempotency-Key` triggers a two-layer check: a Redis sentinel (`SET NX EX`) for fast in-progress detection, and a PostgreSQL `IdempotencyRecord` for durable replay. Replayed responses are bit-for-bit identical to the original — the serialized payload is stored, not recomputed.

- **No overselling.** Stock deduction uses a conditional update: `UPDATE … SET stock = stock - qty WHERE stock >= qty`. If stock is insufficient the update affects zero rows; the service raises a 409 before the transaction commits. No application-level read-then-write race is possible.

- **`transaction.on_commit` discipline.** Every Celery dispatch (`enqueue_authorize_payment`, `enqueue_generate_invoice`) is registered with `on_commit`. A transaction that rolls back never enqueues a task.

- **Two-phase invoice generation.** The `Invoice` row is committed before file I/O starts. The `pdf_url = ""` sentinel is the crash-recovery signal. Phase 2 is idempotent via the status-guarded UPDATE.

- **Cart cache invalidation.** `schedule_cart_cache_invalidation` is called on every cart mutation — add/remove product, coupon changes, address and payment method updates, checkout. Stale cache entries are never served after a write.

---

## 5. Pluggable Payments

The payment system is built around a `PaymentGateway` abstract base class with two surfaces:

**Simple charge path:**
```python
gateway.charge(amount, currency, payment_method_data) -> ChargeResult
```

**Full lifecycle:**
```python
gateway.authorize(...)  -> AuthorizationResult
gateway.capture(...)    -> CaptureResult
gateway.void(...)       -> VoidResult
gateway.refund(...)     -> RefundResult
```

Gateways are registered by slug and resolved at runtime:

```python
register_payment_gateway("stripe", StripeGateway)
gateway = get_payment_gateway("stripe")   # no if/else in service code
```

Three deterministic test gateways are included and registered at startup via `PaymentConfig.ready()`:

| Slug | Behaviour |
|---|---|
| `dummy_success` | Always authorises |
| `dummy_failing` | Always declines (`GatewayDeclined`) |
| `dummy_timeout` | Always raises `GatewayTimeout` |

**To add a real gateway:**
1. Subclass `PaymentGateway` and implement the required methods.
2. Call `register_payment_gateway("your-slug", YourGateway)` — typically in an `AppConfig.ready()`.
3. Add contract tests against the shared gateway test base in `apps/payment/tests/`.
4. Set the `gateway_slug` on the tenant's payment method configuration.

No changes to `CheckoutService`, `PaymentService`, or any task are required. See [`docs/payment-gateways.md`](docs/payment-gateways.md) for the full interface contract.

---

## 6. API Reference

| | URL |
|---|---|
| Swagger UI | `http://localhost:8000/api/docs/` |
| OpenAPI schema | `http://localhost:8000/api/schema/` |
| ReDoc | `http://localhost:8000/api/redoc/` |

### Required headers

| Header | Required on | Notes |
|---|---|---|
| `X-Tenant-Domain` | All tenant-scoped endpoints | Resolved to a `Tenant` row by middleware; missing or unknown domain returns 400 |
| `X-User-Id` | All cart / checkout endpoints | UUID string; the API gateway is responsible for validating the bearer token and injecting this header |
| `Idempotency-Key` | Checkout only | Any unique string (UUID recommended); required for both checkout endpoints |

### Cart endpoints (customer-facing, no `cart_id` required)

```
GET  /api/v1/cart/                    read active cart (created on first access)
POST /api/v1/cart/add-product/
POST /api/v1/cart/remove-product/
POST /api/v1/cart/add-coupon/
POST /api/v1/cart/remove-coupon/
POST /api/v1/cart/add-address/
POST /api/v1/cart/add-payment-method/
POST /api/v1/cart/checkout/
```

The active cart is resolved automatically from `(X-Tenant-Domain, X-User-Id, status=ACTIVE)`. Customers never manage cart identifiers.

### Explicit checkout (internal / admin)

```
POST /api/v1/carts/{cart_id}/checkout/
```

Requires `X-User-Id` to match the cart owner. Use this endpoint from ops tooling or integration tests that track a specific cart ID.

### Health

```
GET /health/    liveness — always 200 if the process is running
GET /ready/     readiness — 200 if Postgres and Redis are reachable; 503 otherwise
GET /healthz    alias for /health/
GET /readyz     alias for /ready/
```

---

## 7. Setup and Commands

### Docker Compose (recommended)

```bash
cp .env.example .env      # review DATABASE_URL / REDIS_URL if needed
make up                   # builds images, starts all services, runs migrations automatically
make seed                 # load demo data — prints a ready-to-run curl checkout command
```

The `web` container runs `migrate --noinput` at startup, so `make migrate` is only needed after `make reset`.

### Makefile reference

**Stack**

| Command | Description |
|---|---|
| `make up` | Build images and start all services (web, worker, db, redis) |
| `make down` | Stop all services |
| `make restart` | `down` then `up` |
| `make logs` | Tail logs for all services |
| `make logs s=web` | Tail logs for a specific service (`web`, `worker`, `db`, `redis`) |
| `make build` | Rebuild images without starting |
| `make reset` | Destroy everything including volumes, rebuild clean |

**Database**

| Command | Description |
|---|---|
| `make migrate` | Apply all pending migrations |
| `make makemigrations` | Generate new migration files |
| `make makemigrations app=invoice` | Generate migrations for a specific app |

**Demo data**

| Command | Description |
|---|---|
| `make seed` | Idempotent seed — tenant, products, coupons, address, payment method, cart |
| `make seed args=--no-cart` | Seed everything except the demo cart |

**Quality**

| Command | Description |
|---|---|
| `make test` | Run the full pytest suite inside the container |
| `make test args='-k checkout'` | Run a subset of tests by keyword |
| `make lint` | Run `ruff` (report only, no auto-fix) |

**Developer tools**

| Command | Description |
|---|---|
| `make shell` | Django shell (`python manage.py shell`) |
| `make shell-db` | `psql` inside the `db` container |
| `make swagger` | Open Swagger UI in the default browser |

### Local (without Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
cp .env.example .env                       # set DATABASE_URL and REDIS_URL
python manage.py migrate
python manage.py runserver                 # http://localhost:8000
python manage.py seed_demo_data
DJANGO_SETTINGS_MODULE=cart_system.settings.test pytest -q
```

### Settings modules

| Module | Used for |
|---|---|
| `cart_system.settings.dev` | Local development — `DEBUG=True`, browsable API, `AllowAny` |
| `cart_system.settings.prod` | Production — HSTS, secure cookies, JSON logging, no fallback secrets |
| `cart_system.settings.test` | Test runs — SQLite fallback, `CELERY_TASK_ALWAYS_EAGER`, fast password hasher |

---

## 8. Trade-offs

| Decision | Why | Accepted cost |
|---|---|---|
| **Shared schema multi-tenancy (`tenant_id` column)** | Operational simplicity — one migration run, zero onboarding cost per tenant, sharding-ready by design | Larger blast radius if the primary DB degrades; per-tenant DB isolation deferred |
| **Redis as Celery broker** | Keeps the deployment to a single additional service; no separate broker to operate for this assessment | For higher-scale production, swap to RabbitMQ — `CELERY_BROKER_URL` is the only required change; payment domain logic is fully decoupled from transport |
| **No real payment gateway** | Clean `PaymentGateway` interface is proven against deterministic dummies before any provider coupling is introduced | Provider-specific error codes, 3DS flows, and webhook verification are not implemented |
| **No aggressive checkout caching** | Checkout reads live database state — no risk of serving a stale stock count or stale coupon validity to a paying customer | Higher DB load per checkout; acceptable at the current scale target |
| **Two-phase invoice generation** | PDF I/O never holds a database lock; crash recovery is clean via the `pdf_url = ""` sentinel | Two database writes per invoice instead of one; the sentinel must be monitored in production |
| **Read replicas and sharding deferred** | "No premature distribution" — one DB is debuggable by one engineer | Vertical scaling ceiling; `tenant_id`-leading indexes and the `all_tenants()` escape hatch are already in place for a future Citus or replica-routing migration |

---

## 9. Observability

`cart_system` ships structured logging and metric hooks with no vendor lock-in. Full details are in [`docs/observability.md`](docs/observability.md).

### Request correlation

Every request carries an `X-Request-Id`. If the client sends one it is preserved; if absent a UUID4 hex string is generated. The ID is bound into a `ContextVar` (`apps.core.context`) so every log record emitted during that request — in middleware, service layer, signal handlers — automatically carries `request_id` without explicit argument passing. The header is echoed on the response.

### Structured logs

Development (human-readable):
```
[2026-05-07 02:30:00] INFO  apps.order.services req=7b2e9c41 tenant=aaa user=111 checkout.completed outcome=success duration_ms=87
```

Production (`prod.py` switches the console handler to JSON):
```json
{"ts":"2026-05-07T02:30:00.123","level":"INFO","logger":"apps.order.services","msg":"checkout.completed","request_id":"7b2e9c41...","tenant_id":"aaa...","outcome":"success","duration_ms":87}
```

### Metric hooks

`apps.core.metrics.incr(name, **labels)` emits metric events as structured log records. No Prometheus or Datadog client is required. Replace `incr()` with a real counter client in production without changing any call site.

Key metrics emitted: `checkout.failed`, `checkout.lock_contention`, `payment.authorized`, `payment.declined`, `payment.timeout`, `idempotency.replay`, `idempotency.conflict`, `invoice.failed`, `readiness.dependency_failed`.

---

## Repository Layout

```text
.
├── PROJECT_SPEC.md            ← architectural contract — read this first
├── README.md
├── Makefile                   ← make up / test / seed / reset / …
├── Dockerfile                 ← dev + prod multi-stage build
├── docker-compose.yml         ← web, worker, db, redis
├── .env.example
├── manage.py
├── requirements.txt
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
    ├── architecture.md
    ├── observability.md
    ├── payment-gateways.md
    └── openapi.yaml
```

Each app follows the same internal layout: `models.py` → `services.py` (all business logic) → `views.py` (thin: parse, validate, dispatch, serialize) → `serializers.py` → `urls.py` → `tests/`.

---

## License

TBD.
