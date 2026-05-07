# Runbook — cart_system

Operational reference for developers and on-call engineers.

---

## Table of Contents

1. [Dev quickstart](#1-dev-quickstart)
2. [Required headers](#2-required-headers)
3. [Key API flows (curl)](#3-key-api-flows-curl)
4. [Health and readiness checks](#4-health-and-readiness-checks)
5. [Celery worker](#5-celery-worker)
6. [Migrations](#6-migrations)
7. [Seed demo data](#7-seed-demo-data)
8. [Rate limiting](#8-rate-limiting)
9. [Common failure modes](#9-common-failure-modes)

---

## 1. Dev Quickstart

```bash
# Clone and enter the repo
cd "zed-task-3"

# Copy env file and adjust secrets if needed
cp .env.example .env

# Start all services (Postgres, Redis, Django, Celery worker)
docker compose up --build

# Apply all migrations (first time or after pulling new code)
docker compose exec web python manage.py migrate

# Seed demo data (idempotent — safe to run multiple times)
docker compose exec web python manage.py seed_demo_data

# Run test suite
docker compose exec web pytest

# Lint
docker compose exec web ruff check .
```

Makefile shortcuts (if available):

```bash
make up       # docker compose up --build
make migrate  # python manage.py migrate
make seed     # python manage.py seed_demo_data
make test     # pytest
```

---

## 2. Required Headers

Every non-exempt endpoint requires all three of these headers:

| Header | Type | Description |
|--------|------|-------------|
| `X-Tenant-Domain` | `string` | Registered tenant domain (e.g. `demo.localhost`). Missing → `400 tenant/missing-header`. Unknown → `404 tenant/not-found`. Inactive → `403 tenant/disabled`. |
| `X-User-Id` | `UUID` | Customer identifier (UUID). This is the interim contract until bearer-token auth is wired at the gateway layer. Missing/invalid → `400 validation/user-id-required`. |
| `Idempotency-Key` | `UUID` | **Required on mutating endpoints** (`checkout` only). Client-generated UUID unique per logical request attempt. Same key + same body = safe retry. |

Optional header:

| Header | Description |
|--------|-------------|
| `X-Request-Id` | Client-supplied correlation ID. Echoed in the response as `X-Request-Id`. Auto-generated if absent. |

Exempt paths (no `X-Tenant-Domain` required):
`/admin/`, `/health/`, `/ready/`, `/healthz`, `/readyz`, `/api/schema/`, `/api/docs/`, `/api/redoc/`

---

## 3. Key API Flows (curl)

Replace `<domain>` with the tenant domain (e.g. `demo.localhost`) and `<user_uuid>` with a customer UUID.

### 3.1 Get active cart

```bash
curl -s http://localhost:8000/api/v1/cart/ \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001"
```

### 3.2 Add a product

```bash
curl -s -X POST http://localhost:8000/api/v1/cart/add-product/ \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{"product_id": "<product_uuid>", "quantity": 2}'
```

### 3.3 Set B2B business details

```bash
curl -s -X POST http://localhost:8000/api/v1/cart/set-business-details/ \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "Acme Corp Ltd",
    "tax_number": "GB123456789",
    "purchase_order_reference": "PO-2026-00042"
  }'
```

### 3.4 Add a shipping address

```bash
curl -s -X POST http://localhost:8000/api/v1/cart/add-address/ \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{"country": "US", "city": "New York", "details": "123 Main St"}'
```

### 3.5 Add a payment method

```bash
curl -s -X POST http://localhost:8000/api/v1/cart/add-payment-method/ \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{"gateway_slug": "dummy_success"}'
```

### 3.6 Checkout

```bash
curl -s -X POST http://localhost:8000/api/v1/cart/checkout/ \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)"
```

### 3.7 Idempotent replay

Repeat the **exact same** `Idempotency-Key` UUID and empty body. The server returns the stored `202` without creating a second order:

```bash
IDEM_KEY="550e8400-e29b-41d4-a716-446655440000"

# First attempt
curl -s -X POST http://localhost:8000/api/v1/cart/checkout/ \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY"

# Safe retry — same key, same response, no new order
curl -s -X POST http://localhost:8000/api/v1/cart/checkout/ \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY"
```

### 3.8 Resource-oriented checkout (with explicit cart_id)

```bash
curl -s -X POST "http://localhost:8000/api/v1/carts/<cart_uuid>/checkout/" \
  -H "X-Tenant-Domain: demo.localhost" \
  -H "X-User-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"payment_method_id": "<pm_uuid>", "address_id": "<addr_uuid>"}'
```

---

## 4. Health and Readiness Checks

```bash
# Liveness — process is alive
curl -s http://localhost:8000/health/ | python -m json.tool
# Expected: {"status": "ok"}

# Readiness — DB + Redis reachable
curl -s http://localhost:8000/ready/ | python -m json.tool
# Expected: {"status": "ok", "db": "ok", "redis": "ok"}

# Kubernetes-style aliases (no trailing slash)
curl -s http://localhost:8000/healthz
curl -s http://localhost:8000/readyz
```

If `/ready/` returns `"redis": "error"`, Redis is unreachable — check `REDIS_URL` in `.env` and that the `redis` Docker service is running.

---

## 5. Celery Worker

The `worker` Docker Compose service starts automatically. To inspect it:

```bash
# View worker logs
docker compose logs -f worker

# Inspect queues
docker compose exec worker celery -A cart_system inspect active_queues

# Purge a queue (use with care)
docker compose exec worker celery -A cart_system purge -Q payments
```

Queues:
- `payments` — `authorize_payment` task (payment gateway call)
- `invoices` — `generate_invoice` task (PDF generation)
- `notifications` — future webhook/email notifications

The worker uses `ALWAYS_EAGER = True` in test settings so Celery tasks run synchronously and do not require a real broker in tests.

---

## 6. Migrations

```bash
# Apply all pending migrations
docker compose exec web python manage.py migrate

# Show migration status
docker compose exec web python manage.py showmigrations

# Create a new migration after model changes
docker compose exec web python manage.py makemigrations <app_name>
```

Recent notable migrations:
- `cart/0006_cart_b2b_fields` — adds `company_name`, `tax_number`, `purchase_order_reference`
- `order/0003_order_b2b_fields` — mirrors B2B fields on Order as snapshot

---

## 7. Seed Demo Data

```bash
# Full seed (creates tenant, products, coupons, and a demo cart)
docker compose exec web python manage.py seed_demo_data

# Skip cart creation
docker compose exec web python manage.py seed_demo_data --no-cart

# Verbose output
docker compose exec web python manage.py seed_demo_data --verbosity 2
```

Demo tenant: `demo.localhost`
Demo customer UUID: printed at the end of the command output.

---

## 8. Rate Limiting

Throttle is applied per `(tenant, user, action)` using Redis INCR + EXPIRE (fixed window). Counters are stored in Redis — not per-process memory — so all app containers share the same counter.

| Endpoint | Scope | Limit |
|----------|-------|-------|
| `POST /api/v1/cart/checkout/` | `checkout` | 10 / minute |
| `POST /api/v1/carts/{id}/checkout/` | `checkout` | 10 / minute |
| `POST /api/v1/cart/add-product/` | `add_product` | 60 / minute |

A throttled request returns:

```json
{
  "type": "https://cart-system.local/problems/rate-limit/exceeded",
  "title": "Too many requests",
  "status": 429,
  "detail": "Rate limit exceeded — retry after the window resets."
}
```

To adjust limits, edit `DEFAULT_THROTTLE_RATES` in `cart_system/settings/base.py` and redeploy.

---

## 9. Common Failure Modes

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `400 tenant/missing-header` on every request | `X-Tenant-Domain` header absent | Add the header to all API calls |
| `404 tenant/not-found` | Domain not registered in DB | Run `seed_demo_data` or create tenant in Django admin |
| `422 cart/checkout-incomplete` | No address or payment method selected | Call `add-address` and `add-payment-method` before checkout |
| `409 cart/locked` | Redis lock held by concurrent checkout | Retry with back-off (~1 s) |
| `409 idempotency/conflict` | Same `Idempotency-Key` with different body | Use a fresh UUID key |
| `422 product/out-of-stock` | Stock depleted between add-to-cart and checkout | Remove the item or reduce quantity |
| `500` on checkout | Redis not reachable | Check `REDIS_URL`; verify `redis` service is healthy |
| Worker not processing payments | Celery broker down | Restart `worker` service; verify Redis is up |
| Migrations fail with column already exists | Migration partially applied | Check `django_migrations` table; squash if necessary |
| `429` on checkout / add-product | Rate limit exceeded | Wait for the 1-minute window to reset; adjust limits in settings if needed |
