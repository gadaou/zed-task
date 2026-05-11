# Testing Guide

This document explains the `cart_system` test strategy, how to run the suite, and why concurrency and idempotency coverage is central to correctness.

---

## How to Run Tests

### Prerequisites

The project uses a `.venv` virtual environment at the repo root.

```bash
# Create venv if needed, then install runtime, test, and lint tooling (includes pinned Ruff)
python3 -m venv .venv
pip install -r requirements-dev.txt

# Activate the venv (optional — all commands below prefix it explicitly)
source .venv/bin/activate
```

If your local `.env` points `DATABASE_URL` to PostgreSQL, either start Postgres through Docker Compose or override `DATABASE_URL=sqlite:///:memory:` for the fast local test suite.

### Run the full suite

```bash
.venv/bin/python -m pytest -q
# or, if the venv is activated:
pytest -q
```

Expected result: **393 passed, 5 skipped, 0 failures** (398 nodes collected; the difference from 380 `def test_*` functions comes from `pytest.mark.parametrize` expansion). The 5 skips are intentional. `PaymentGatewayContractTests` calls `pytest.skip()` when a contract branch does not apply to the concrete gateway implementation (for example, a decline-only assertion on `DummySuccessGateway`).

### Filter to a specific domain or test name

```bash
# Run only cart tests
pytest apps/cart/ -q

# Run only checkout-related tests across all apps
pytest -k checkout -q

# Run only transaction-safe tests (those that touch SELECT FOR UPDATE)
pytest -m django_db -q

# Run a single test by full node ID
pytest apps/order/tests/test_services.py::test_checkout_happy_path -v
```

### Run with verbose output and stop on first failure

```bash
pytest -x -v
```

### Make targets (Docker-based)

```bash
make test              # full suite inside the container
make test args='-k checkout'   # subset matching a keyword
```

---

## Test Configuration

| Setting | Value |
|---------|-------|
| Config file | `pytest.ini` |
| Django settings | `cart_system.settings.test` |
| Database | SQLite (in-memory) unless `DATABASE_URL` is set |
| Redis | `fakeredis` (monkeypatched per test) |
| Celery | `CELERY_TASK_ALWAYS_EAGER = True` (tasks run synchronously) |
| Password hasher | MD5 (fast for tests) |

All tests that touch `SELECT FOR UPDATE` or `transaction.on_commit` are marked `@pytest.mark.django_db(transaction=True)` to ensure real transactions run — not the default savepoint wrapping.

---

## What Is Tested

### Tenant Isolation

**Files:** `apps/tenant/tests/`, `apps/cart/tests/test_services.py`, `apps/cart/tests/test_views.py`

Every domain model is a `TenantAwareModel`; the `TenantAwareManager` injects `WHERE tenant_id = <current>` into every queryset. Tests verify:

- `TenantMiddleware` resolves the tenant from `X-Tenant-Domain` and activates context; rejects unknown or missing headers.
- `TenantAwareManager` raises `TenantContextMissing` outside a context; `.all_tenants()` bypasses the filter for admin use.
- The same `user_id` can hold one `ACTIVE` cart per tenant with no collision.
- `GET /api/v1/carts/{id}/checkout/` under tenant A with a cart from tenant B returns 404 — no cross-tenant data leaks.
- `POST /api/v1/cart/items/` with a product from tenant B returns 404 under tenant A.

### Cart Add / Remove Product

**Files:** `apps/cart/tests/test_views.py`, `apps/cart/tests/test_services.py`

- Adding a product creates a `CartItem` with a price snapshot and bumps `cart.version`.
- Adding the same product again increments quantity on the existing row (no duplicate rows; enforced by `uq_cartitem_cart_product`).
- Removing a product deletes the line and zeroes the running total.
- Removing a product not on the cart is idempotent (no error).
- Zero quantity and missing fields are rejected 400.
- Unknown `product_id` returns 404 `product/not-found`.

### Coupon Apply / Remove

**Files:** `apps/cart/tests/test_views.py`, `apps/coupon/tests/test_services.py`, `apps/coupon/tests/test_validator.py`

- `PERCENTAGE` and `FIXED` discount types apply correctly; `FIXED` is clamped at the remaining payable amount.
- Two distinct coupons stack the discounts (per stacking policy).
- Removing an applied coupon deletes the row, decrements `used_count`, and recalculates totals.
- Removing a coupon not on the cart is idempotent.

### Coupon Constraints and Race Conditions

**Files:** `apps/coupon/tests/test_services.py`

- `min_total` not met → `CouponConstraintFailed`; `used_count` unchanged.
- `allowed_countries` mismatch → `CouponConstraintFailed`.
- `usage_limit` at cap → `CouponLimitReached`.
- Expired validity window → `CouponExpired`.
- Inactive coupon → `CouponNotFound`.
- Re-applying the same coupon → `CouponAlreadyApplied`; `used_count` unchanged.
- **Stacking policy:** `ONE_PER_DISCOUNT_TYPE` allows PERCENTAGE + FIXED but rejects PERCENTAGE + PERCENTAGE; `SINGLE_ONLY` rejects any second coupon; `UNLIMITED` allows arbitrary stacking.
- **Concurrency:** a conditional `UPDATE WHERE used_count < usage_limit` refuses the increment when the cap is reached concurrently. A simulated race where the validator passed but another committer bumped `used_count` in between causes the second commit to be refused.
- **Revalidation at checkout:** all applied coupons are revalidated; a coupon that expired, was deactivated, or whose constraint state drifted since apply raises the appropriate error and rolls back the entire checkout.

### Checkout Happy Path

**Files:** `apps/order/tests/test_services.py`, `apps/order/tests/test_views.py`, `apps/cart/tests/test_views.py`

A full checkout (`CheckoutService.checkout`) verifies:

- `Order` created with correct `total`, `currency`, `idempotency_key`, and `status = PENDING_PAYMENT`.
- `OrderItem` rows copied from `CartItem`s with price snapshots.
- Product stock decremented atomically.
- `Cart.status` set to `CHECKED_OUT`.
- `Payment` created in `REQUIRES_CONFIRMATION` status.
- The payment dispatcher (`enqueue_authorize_payment`) is called exactly once, and only after the database transaction commits (`on_commit` guard).

Both the action endpoint (`POST /api/v1/cart/checkout/`) and the legacy resource endpoint (`POST /api/v1/carts/{cart_id}/checkout/`) are exercised.

### Duplicate Checkout / Idempotency

**Files:** `apps/order/tests/test_services.py`, `apps/order/tests/test_views.py`, `apps/cart/tests/test_views.py`

- Replaying the same `Idempotency-Key` returns the stored 202 response without creating a second order.
- Sending the same key with a different request body (different address or payment method) returns 409 `idempotency/conflict`.
- A concurrent duplicate call while the first is still in-progress returns 409 `idempotency/in-progress`.
- Missing `Idempotency-Key` header → 400 `validation/idempotency-key-required`.
- Non-UUID `Idempotency-Key` → 400 `validation/idempotency-key-invalid`.

### Stock Race Handling

**Files:** `apps/order/tests/test_services.py`

- Requesting more quantity than `product.stock` → `ProductOutOfStock` raised; stock is **not** decremented and the transaction is rolled back.
- A cart whose version was bumped by a concurrent request between the read and the update step → `CartStaleVersion` raised.
- A pre-existing Redis lock on the cart key → `LockNotAcquired` raised.
- On any failure that rolls back the transaction, the payment dispatcher is **not** called (the `on_commit` hook does not fire on rollback).

The action checkout endpoint maps `ProductOutOfStock` → 422 `product/out-of-stock` (`apps/cart/tests/test_views.py::test_checkout_oos_returns_422`).

### Payment Success / Failure / Timeout

**Files:** `apps/payment/tests/test_services.py`, `apps/payment/tests/test_gateways.py`, `apps/payment/tests/test_charge.py`, `apps/payment/tests/test_process_payment.py`

`PaymentService.authorize_payment` is tested for every terminal outcome:

| Outcome | Gateway | Payment status | Order status |
|---------|---------|---------------|--------------|
| Success | `DummySuccessGateway` | `AUTHORIZED` | `PAID` |
| Decline | `DummyFailingGateway` | `FAILED` | `FAILED` |
| Timeout | `DummyTimeoutGateway` | `REQUIRES_CONFIRMATION` | unchanged |
| Unknown slug | — | unchanged | unchanged, `UnsupportedGateway` raised |

Additional:
- Idempotent re-authorization: a second call with the same `payment_id` returns immediately without contacting the gateway again.
- Invoice is enqueued on success; **not** enqueued on decline.
- Capture, void, and refund FSM transitions are exercised.
- The Celery `authorize_payment` task is idempotent under double delivery; marks payment `FAILED` on `UnsupportedGateway` without retry.
- The Celery `process_payment` task handles success, idempotent double call, order-not-found, no-payment, decline, and timeout propagation.
- Every `PaymentGateway` implementation is verified against the shared `PaymentGatewayContractTests` mixin (correct return types, non-empty slug, metadata acceptance).

### Invoice Generation / Retry

**Files:** `apps/invoice/tests/test_services.py`, `apps/invoice/tests/test_tasks.py`

- A `PAID` order produces an `Invoice` with correct fields, sequential number, and a PDF file under `MEDIA_ROOT/invoices/`.
- Taxes are computed as `total × 15%` (ROUND_HALF_UP, 2 decimal places).
- If PDF rendering raises, the `Invoice` DB row is committed with `pdf_url=""` and the sequence is **not** double-advanced.
- Calling again after a PDF-only failure renders the PDF and updates `pdf_url` without allocating a new invoice number (retry path).
- Calling after a fully-generated invoice returns the same object without re-rendering (full idempotency).
- Invoice numbers are monotonically increasing per tenant and reset at 1 for a new tenant (cross-tenant isolation).
- The Celery `generate_invoice` task is idempotent under redelivery; runs on the `invoices` queue.

### Cart Cache Invalidation

**Files:** `apps/cart/tests/test_cache.py`

The cart read-through cache (Redis-backed, toggled by `CART_CACHE_ENABLED`) is exercised across 12 scenarios:

- `GET /api/v1/cart` populates the cache on miss; second GET is a cache hit (no extra DB row).
- Every mutating service (`add_product_to_cart`, `remove_product_from_cart`, `apply_coupon_to_cart`, `remove_coupon_from_cart`, `set_cart_address`, `set_cart_payment_method`) invalidates the cache via the `on_commit` hook.
- A successful checkout invalidates the cache; a subsequent `GET /cart` creates a fresh ACTIVE cart and re-populates the cache.
- A rolled-back checkout (e.g. `CartEmpty`) does **not** fire `on_commit` and therefore leaves the cached entry intact.
- When Redis raises, `GET /cart` falls back to a fresh Postgres read and returns 200 (graceful degradation).
- With `CART_CACHE_ENABLED=False`, `set_cart_cache` and `get_cart_cache` are no-ops; `GET /cart` returns 200 via Postgres.

### Health / Readiness Endpoints

**Files:** `apps/core/tests/test_health_endpoints.py`

- `GET /health/` returns 200 `{"status": "ok", "service": "cart-system"}` with no dependency checks (liveness probe).
- `GET /ready/` returns 200 with `checks.postgres = "ok"` and `checks.redis = "ok"` when both are reachable (readiness probe).
- `GET /ready/` returns 503 `{"status": "unavailable"}` when Redis ping raises (Redis failure is surfaced in `checks.redis`).
- The OpenAPI schema (`GET /api/schema/`) exposes both `/health/` and `/ready/` paths.

---

## Why Concurrency and Idempotency Tests Matter

### Concurrency

The checkout flow reads the cart, validates stock, deducts stock, and marks the cart as checked-out — all as a sequence of dependent reads and writes. Without a lock, two simultaneous requests for the same cart can both read the same stock count, both decide there is enough stock, and both deduct from it, leaving the system in an oversold state.

The system uses two layers of protection:

1. **Redis distributed lock** (`apps/core/locks.redis_lock`) — acquired before the database transaction opens. A second concurrent request fails to acquire the lock and receives `LockNotAcquired` (mapped to 409) before touching the database.
2. **PostgreSQL `SELECT FOR UPDATE`** (`lock_active_cart_for_update`) — serializes concurrent writes at the row level within a transaction.
3. **Optimistic version check** (`CartStaleVersion`) — if two requests race past the Redis lock, the one that commits second detects the version mismatch and fails safely.

Tests for `LockNotAcquired`, `CartStaleVersion`, and `ProductOutOfStock` (with rollback verification and dispatcher suppression) prove that none of these failure modes silently corrupt data.

### Idempotency

Network retries, client timeouts, and load-balancer replays mean any checkout endpoint will receive duplicate requests. Without idempotency, a retry creates a second order, charges the customer twice, and generates duplicate invoices.

The system assigns each checkout attempt a client-supplied `Idempotency-Key` UUID. The first request acquires an in-progress Redis marker (`SET NX`), performs the checkout, and stores the final response in an `IdempotencyRecord`. Any subsequent request with the same key returns the stored response immediately without re-executing the checkout.

Tests for idempotent replay (same key → same 202), conflict (same key + different body → 409), and in-progress (concurrent NX attempt → 409) prove the guarantee holds across all three paths a duplicate can take. Invoice generation carries the same guarantee: re-delivering the Celery task never creates a second invoice or advances the sequence twice.

These behaviors are difficult to validate by inspection alone; they need tests that recreate race windows and replay scenarios that show up under real load.
