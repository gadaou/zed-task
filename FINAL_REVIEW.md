# Final Review — cart_system

> Submission-readiness checklist and validation guide for the multi-tenant
> cart and checkout system.

---

## Feature Checklist

| # | Feature | Status | Key files |
|---|---------|--------|-----------|
| 1 | Multi-tenant isolation (`X-Tenant-Domain` header, `TenantAwareManager`, partial unique index) | ✓ Complete | `apps/tenant/`, `apps/cart/migrations/0005_*` |
| 2 | Cart CRUD — add/remove product, apply/remove coupon | ✓ Complete | `apps/cart/views.py`, `apps/cart/services.py` |
| 3 | Optimistic concurrency (`Cart.version`, `F("version") + 1`) | ✓ Complete | `apps/cart/models.py`, `apps/cart/services.py` |
| 4 | Checkout with distributed Redis lock + Postgres row lock | ✓ Complete | `apps/order/services.py` |
| 5 | Idempotent checkout (`Idempotency-Key` header, `IdempotencyRecord`) | ✓ Complete | `apps/core/idempotency.py`, `apps/core/models.py` |
| 6 | Async payment dispatch (Celery `authorize_payment` task, `on_commit` hook) | ✓ Complete | `apps/payment/tasks.py`, `apps/order/services.py` |
| 7 | Invoice generation (Celery task, ReportLab PDF) | ✓ Complete | `apps/invoice/services.py`, `apps/invoice/tasks.py` |
| 8 | Redis read-through cache for `GET /cart/` | ✓ Complete | `apps/core/cache.py` |
| 9 | RFC 7807 problem+json error responses | ✓ Complete | `apps/core/responses.py`, `apps/tenant/exceptions.py` |
| 10 | Health (`/health/`) and readiness (`/ready/`) endpoints | ✓ Complete | `apps/core/urls.py`, `apps/core/views.py` |
| 11 | Swagger UI at `/api/docs/` with all headers and examples | ✓ Complete | `cart_system/urls.py`, `apps/core/openapi.py` |
| 12 | `seed_demo_data` management command (idempotent) | ✓ Complete | `apps/core/management/commands/seed_demo_data.py` |
| 13 | Docker Compose (db, redis, web, worker) | ✓ Complete | `docker-compose.yml` |
| 14 | **B2B support** (`company_name`, `tax_number`, `purchase_order_reference`) | ✓ Complete | `apps/cart/models.py`, `apps/order/models.py`, `apps/cart/views.py` |
| 15 | **`POST /api/v1/cart/set-business-details/`** action | ✓ Complete | `apps/cart/views.py`, `apps/cart/services.py` |
| 16 | **B2B fields snapshotted on Order at checkout** | ✓ Complete | `apps/order/services.py` |
| 17 | **Redis-backed rate limiting** on checkout + add-product (per tenant/user) | ✓ Complete | `apps/core/throttling.py`, `apps/cart/views.py`, `apps/order/views.py` |
| 18 | **429 documented in Swagger** for throttled endpoints | ✓ Complete | `apps/cart/views.py`, `apps/order/views.py` |

---

## Known Constraints / Out of Scope

| Item | Notes |
|------|-------|
| Bearer-token auth | API gateway is the enforcement point; views trust `X-User-Id` header as an interim contract (PROJECT_SPEC §4.3). |
| Catalog microservice | `Product` is a local model; real catalog integration uses a remote call. |
| Row-level security (RLS) | Tenant isolation is enforced at the application layer via `TenantAwareManager`; Postgres RLS is the roadmap item (PROJECT_SPEC §9.7). |
| Real payment gateways | Stripe / HyperPay / Tabby are stubbed with `DummySuccessGateway`. |
| Address book CRUD | Addresses are created inline by `POST /cart/add-address/`; a standalone address management API is out of scope. |
| Order listing / status polling | `GET /api/v1/orders/{id}/` is not wired in this iteration. |

---

## Test Summary

26 test files across 9 apps:

| App | Test files | Key coverage |
|-----|-----------|--------------|
| `cart` | `test_views.py`, `test_services.py`, `test_cache.py`, **`test_b2b.py`**, **`test_throttling.py`** | All action endpoints, cart service layer, Redis cache, B2B fields, rate limiting |
| `order` | `test_views.py`, `test_services.py`, `test_checkout_logging.py` | Checkout flow, idempotency, concurrency, structured logs |
| `payment` | `test_services.py`, `test_charge.py`, `test_gateways.py`, `test_add_payment_method.py`, `test_process_payment.py`, `test_registry.py` | Gateway registry, charge, process payment |
| `coupon` | `test_services.py`, `test_validator.py` | Coupon apply/remove/revalidate |
| `invoice` | `test_services.py`, `test_tasks.py` | PDF generation, Celery task |
| `tenant` | `test_middleware.py`, `test_managers.py`, `test_models.py` | Tenant isolation, middleware errors |
| `core` | `test_health_endpoints.py`, `test_responses.py`, `test_request_id_middleware.py`, `test_seed_demo_data.py` | Health/ready, RFC 7807 responses, seed command |
| `addresses` | `test_services.py` | Address creation |

---

## Validation Sequence

Run these commands in order to verify the full stack:

```bash
# 1. Start all services
docker compose up --build -d

# 2. Apply migrations
docker compose exec web python manage.py migrate

# 3. Seed demo data
docker compose exec web python manage.py seed_demo_data

# 4. Run test suite
docker compose exec web pytest

# 5. Lint check
docker compose exec web ruff check .

# 6. Health / readiness
curl -s http://localhost:8000/health/ | python -m json.tool
curl -s http://localhost:8000/ready/  | python -m json.tool

# 7. Swagger UI (open in browser)
open http://localhost:8000/api/docs/

# 8. Raw OpenAPI schema (JSON)
curl -s http://localhost:8000/api/schema/ | python -m json.tool | head -40
```

### Key API flows (demo tenant `demo.localhost`, user `00000000-0000-0000-0000-000000000001`)

```bash
BASE="http://localhost:8000/api/v1/cart"
H='-H "X-Tenant-Domain: demo.localhost" -H "X-User-Id: 00000000-0000-0000-0000-000000000001"'

# Get active cart
curl -s $BASE/ $H | python -m json.tool

# Set B2B details
curl -s -X POST $BASE/set-business-details/ $H \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Acme Corp", "purchase_order_reference": "PO-001"}' \
  | python -m json.tool

# Add a product (product UUID from seed_demo_data output)
curl -s -X POST $BASE/add-product/ $H \
  -H "Content-Type: application/json" \
  -d '{"product_id": "<uuid>", "quantity": 2}' \
  | python -m json.tool

# Checkout (after add-address + add-payment-method)
curl -s -X POST $BASE/checkout/ $H \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  | python -m json.tool
```
