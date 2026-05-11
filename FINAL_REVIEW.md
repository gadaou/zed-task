# Final Review — cart_system

> Submission-readiness audit against the original Zid assignment.
> Every claim is tied to concrete source files, migrations, or tests.

---

## 1. Original Requirements

| Requirement | Implementation | Main files / modules | Test coverage | Status |
|---|---|---|---|---|
| **Python + Django** | Python 3.11, Django 5.1, DRF 3.15 | `requirements.txt`, `Dockerfile`, `cart_system/settings/base.py` | All tests run under this stack | ✓ Complete |
| **Single database for all tenants** | Shared-schema PostgreSQL — one `cart_system` DB, one connection pool, `tenant_id` column on every model | `docker-compose.yml` (single `db` service), all app `models.py` | `apps/tenant/tests/test_models.py`, migration files | ✓ Complete |
| **Proper tenant isolation** | Three independent layers: `TenantMiddleware` (resolves tenant from `X-Tenant-Domain`, aborts on missing/unknown/inactive), `TenantAwareManager` (auto-filters every queryset, raises `TenantContextMissing` when context unset), schema-level composite indexes leading with `tenant_id` | `apps/tenant/middleware.py`, `apps/tenant/managers.py`, `apps/tenant/context.py`, per-app migrations | `apps/tenant/tests/test_middleware.py`, `test_managers.py`, `test_models.py`; cross-tenant isolation asserted in `apps/cart/tests/test_views.py` and `test_services.py` | ✓ Complete |
| **Pluggable payment system** | `PaymentGateway` ABC with `authorize_payment`, `capture_payment`, `void_payment`, `refund_payment`; slug-keyed registry; domain code never imports a gateway directly; three deterministic dummy gateways ship with the codebase | `apps/payment/gateways/base.py`, `apps/payment/gateways/registry.py`, `apps/payment/gateways/dummy.py`, `apps/payment/services.py` | `apps/payment/tests/test_gateways.py` (shared `PaymentGatewayContractTests` mixin), `test_services.py`, `test_registry.py`, `test_charge.py` | ✓ Complete |
| **Cart: add product** | `POST /api/v1/cart/items/` — adds or increments a `CartItem`; price snapshot captured at add-time; `Cart.version` bumped; cache invalidated. Legacy alias `POST /api/v1/cart/add-product/` preserved. | `apps/cart/views.py` (`CartItemsView`, `AddProductView`), `apps/cart/services.py` (`add_product_to_cart`) | `apps/cart/tests/test_views.py`, `test_views_rest.py`, `test_services.py` | ✓ Complete |
| **Cart: remove product** | `DELETE /api/v1/cart/items/{product_id}/` — deletes `CartItem`, recalculates totals; idempotent. Legacy alias `POST /api/v1/cart/remove-product/` preserved. | `apps/cart/views.py` (`CartItemDetailView`, `RemoveProductView`), `apps/cart/services.py` (`remove_product_from_cart`) | `apps/cart/tests/test_views.py`, `test_views_rest.py`, `test_services.py` | ✓ Complete |
| **Cart: add coupon** | `POST /api/v1/cart/coupons/` — validates constraints, computes discount snapshot, enforces stacking policy, increments `used_count` with conditional UPDATE. Legacy alias `POST /api/v1/cart/add-coupon/` preserved. | `apps/cart/views.py` (`CartCouponsView`, `ApplyCouponView`), `apps/coupon/services.py` (`CouponService.apply_coupon_to_cart`), `apps/coupon/validators.py` | `apps/coupon/tests/test_services.py`, `test_validator.py`, `test_views_rest.py` | ✓ Complete |
| **Cart: remove coupon** | `DELETE /api/v1/cart/coupons/{coupon_id}/` — removes `CartCoupon` row, decrements `used_count` (guarded `WHERE used_count > 0`), recalculates totals; idempotent. Legacy alias `POST /api/v1/cart/remove-coupon/` preserved. | `apps/cart/views.py` (`CartCouponDetailView`, `RemoveCouponView`), `apps/coupon/services.py` (`remove_coupon_from_cart`) | `apps/coupon/tests/test_services.py`, `test_views_rest.py` | ✓ Complete |
| **Cart: add payment method** | `PUT /api/v1/cart/payment-method/` — creates `PaymentMethod` for the requested gateway slug, sets it as `Cart.selected_payment_method`; returns 422 for unknown slug. Legacy alias `POST /api/v1/cart/add-payment-method/` preserved. | `apps/cart/views.py` (`CartPaymentMethodView`, `AddPaymentMethodView`), `apps/payment/services.py` (`add_payment_method`) | `apps/payment/tests/test_add_payment_method.py`, `apps/cart/tests/test_views.py`, `test_views_rest.py` | ✓ Complete |
| **Cart: add address** | `PUT /api/v1/cart/address/` — creates `Address` record, sets it as `Cart.selected_address`. Legacy alias `POST /api/v1/cart/add-address/` preserved. | `apps/cart/views.py` (`CartAddressView`, `AddAddressView`), `apps/addresses/services.py` (`add_address`) | `apps/addresses/tests/test_services.py`, `apps/cart/tests/test_views.py`, `test_views_rest.py` | ✓ Complete |
| **Cart: checkout** | `POST /api/v1/cart/checkout/` (active-cart) and `POST /api/v1/carts/{cart_id}/checkout/` (explicit) — full 12-step protocol: idempotency check → Redis lock → `transaction.atomic` → coupon revalidation → conditional stock deduction → order + payment creation → `IdempotencyRecord` write → commit → lock release → `on_commit` Celery dispatch | `apps/cart/views.py` (`CartCheckoutView`), `apps/order/views.py` (`CheckoutView`), `apps/order/services.py` (`CheckoutService`) | `apps/order/tests/test_services.py`, `test_views.py`, `apps/cart/tests/test_views.py` | ✓ Complete |
| **Race condition handling** | Three layers: Redis `SET NX PX` distributed lock (fenced Lua release) wraps the entire critical section; PostgreSQL `SELECT FOR UPDATE` on cart, coupon, and payment rows inside the transaction; conditional stock UPDATE (`WHERE stock >= qty`); optimistic `Cart.version` guard | `apps/core/locks.py` (Redis lock), `apps/order/services.py`, `apps/cart/services.py`, `apps/coupon/services.py` | `apps/order/tests/test_services.py` (lock, stale-version, OOS races), `apps/coupon/tests/test_services.py` (concurrent usage-limit race) | ✓ Complete |
| **Documentation** | README, RUNBOOK, TESTING, FINAL_REVIEW, PROJECT_SPEC, `docs/architecture.md`, `docs/observability.md`, `docs/payment-gateways.md`, `docs/test-quality-summary.md`, 8 Mermaid diagrams under `docs/diagrams/` | All listed files | n/a | ✓ Complete |
| **Tests** | 380 test functions across 27 test files (33 tests in `test_views_rest.py` covering canonical RESTful endpoints and legacy regression guards); **393 passed, 5 skipped, 0 failed** | All `apps/*/tests/` directories | See §3 Test Summary below | ✓ Complete |

### Bonus features

| Requirement | Implementation | Main files / modules | Test coverage | Status |
|---|---|---|---|---|
| **Invoice handling** | Celery `generate_invoice` task (queue `invoices`) with two-phase generation: `InvoiceSequence` counter + `Invoice` row committed atomically; PDF rendered outside the transaction; idempotent via `OneToOneField(order)` + status-guarded `pdf_url` UPDATE; ReportLab PDF | `apps/invoice/services.py`, `apps/invoice/tasks.py`, `apps/invoice/pdf.py`, `apps/invoice/models.py` | `apps/invoice/tests/test_services.py` (12 tests), `test_tasks.py` (5 tests) | ✓ Complete |
| **Coupon constraints** | Rule-registry pattern: constraints stored as JSON on `Coupon.constraints`; `CouponValidator` dispatches each key to a registered handler; built-in rules: `min_total`, `allowed_countries`, `usage_limit`; validity window and `is_active` are always-on checks; unknown keys fail closed | `apps/coupon/validators.py`, `apps/coupon/models.py` (`constraints` JSONField) | `apps/coupon/tests/test_validator.py` (31 tests), `test_services.py` | ✓ Complete |
| **B2B orders** | Lightweight metadata: `company_name`, `tax_number`, `purchase_order_reference` fields on `Cart` (set via `PUT /api/v1/cart/business-details/`; legacy alias `POST /api/v1/cart/set-business-details/` preserved as deprecated) and snapshotted onto `Order` at checkout; printed on PDF invoice. Full procurement features (approval workflows, net-N terms, multi-line splits) are intentionally out of scope for this iteration — see Known Constraints. | `apps/cart/models.py` (B2B fields), `apps/order/models.py` (snapshot), `apps/cart/views.py` (`CartBusinessDetailsView`, `SetBusinessDetailsView`), `apps/cart/services.py` (`set_business_details`), `apps/order/services.py` (checkout snapshot) | `apps/cart/tests/test_b2b.py` (9 tests) | ✓ Complete |

---

## 2. Architecture Claims Verified

| Claim | Evidence |
|---|---|
| **PostgreSQL is the sole source of truth** | All domain models persist to Postgres; Redis holds no durable state (locks and idempotency sentinels are ephemeral). If Redis flushes, in-flight checkouts see transient 409s but no data is lost. `IdempotencyRecord` rows survive Redis restarts. Confirmed in `docker-compose.yml` (one `db` service), `cart_system/settings/base.py` (`DATABASES`), and `docs/architecture.md` §Trade-offs. |
| **Redis is used for: distributed locks, idempotency sentinels, cart read cache, rate limiting, AND Celery broker** | Locks: `apps/core/locks.py` (`redis_lock`, Lua fenced release). Idempotency sentinel: `apps/core/idempotency.py` (`SET NX EX`). Cart cache: `apps/core/cache.py` (`get_cart_cache` / `set_cart_cache`). Rate limiting: `apps/core/throttling.py` (`TenantUserScopedThrottle`, Redis INCR + EXPIRE). Celery broker: `CELERY_BROKER_URL = redis://redis:6379/1` in `docker-compose.yml`. Five distinct roles, all in code. |
| **Celery handles async payment and invoice tasks** | `apps/payment/tasks.py` defines `authorize_payment` (queue `payments`). `apps/invoice/tasks.py` defines `generate_invoice` (queue `invoices`). Both are dispatched via `transaction.on_commit` — never orphaned on rollback. Confirmed in `cart_system/celery.py` and `docker-compose.yml` worker command. |
| **No real external payment gateway is intentionally integrated** | Only `DummySuccessGateway`, `DummyFailingGateway`, `DummyTimeoutGateway` are registered. Adding a real gateway requires subclassing `PaymentGateway` and registering the slug — no core changes. Provider-specific concerns (3DS, webhook verification, partial capture quirks) are explicitly out of scope. Stated in `README.md` §5, `docs/architecture.md` §Trade-offs, and `docs/payment-gateways.md` §1. |
| **B2B is lightweight metadata, not a full procurement system** | Three optional string fields (`company_name`, `tax_number`, `purchase_order_reference`) on `Cart` and `Order`. No approval workflows, no net-N payment terms, no multi-line shipping splits. The Known Constraints table below and `PROJECT_SPEC.md` §Bonus features explicitly scope these to future iterations. |
| **Future scale path mentions read replicas, sharding, RabbitMQ/Kafka, and RLS** | Read replicas: `docs/architecture.md` §13 ("Read scaling"), `PROJECT_SPEC.md` §9.1. Sharding by `tenant_id`: `docs/architecture.md` §13 ("Write scaling and sharding"), `PROJECT_SPEC.md` §9.2. RabbitMQ/Kafka (event-driven evolution): `docs/architecture.md` §13 ("Event-driven evolution"), `PROJECT_SPEC.md` §9.3. PostgreSQL RLS: `docs/architecture.md` §3.2 (roadmap note), `PROJECT_SPEC.md` §9.7. |

---

## 3. Unsupported Claims Check

| Check | Finding |
|---|---|
| **No claim of proven 10M-user capacity** | No such phrase found anywhere in the codebase, README, or any doc file. SLOs in `PROJECT_SPEC.md` §1 are stated as "initial targets; revisit after first month of production data." |
| **No claim of full authentication if only `X-User-Id` is used** | All user-facing docs correctly describe `X-User-Id` as an interim contract pending API-gateway bearer-token integration. Specific disclosures: `README.md` §6 ("injected by the API gateway after token validation"), `RUNBOOK.md` §2 ("interim contract until bearer-token auth is wired at the gateway layer"), `apps/cart/views.py` module docstring ("ADR-NOTE: PROJECT_SPEC §4.3 — auth is delegated to the API gateway"). `PROJECT_SPEC.md` §4.3 and §5.4 describe JWT as the **target** state, explicitly aspirational and labelled as such. No doc claims Django currently enforces bearer tokens. |
| **No claim of full procurement / B2B workflow** | B2B is represented as lightweight snapshot metadata everywhere in the submission docs. `PROJECT_SPEC.md` §Bonus features describes approval workflows, net-N terms, and multi-line splits as "later iterations." `FINAL_REVIEW.md` §Known Constraints (below) lists these as explicitly out of scope. |
| **No claim of real payment gateway integration** | Stripe, HyperPay, Tabby, and other real providers are mentioned only as illustrative examples (registry pattern doc in `README.md` §5, extension skeleton in `docs/payment-gateways.md` §5) or as "out of scope" (`README.md` §4 Trade-offs, `PROJECT_SPEC.md` §8). The `docs/openapi.yaml` static file that contained a misleading "Stripe card" example label and a spurious `cookieAuth` security scheme has been **deleted**; the live schema at `/api/schema/` is the authoritative source. |

---

## 4. Test Summary

**27 test files across 8 apps — `393 passed, 5 skipped, 0 failed`**

(`test_views_rest.py` adds 33 tests covering canonical RESTful endpoints and legacy regression guards.)

| App | Test files | Key coverage |
|-----|-----------|--------------|
| `cart` | `test_views.py`, `test_views_rest.py`, `test_services.py`, `test_cache.py`, `test_b2b.py`, `test_throttling.py` | All action endpoints, canonical RESTful endpoints, legacy regression guards, cart service layer, Redis cache invalidation, B2B fields, rate limiting |
| `order` | `test_views.py`, `test_services.py`, `test_checkout_logging.py` | Checkout flow, idempotency (replay / conflict / in-progress), concurrency (lock, stale-version, OOS), structured logs |
| `payment` | `test_services.py`, `test_charge.py`, `test_gateways.py`, `test_add_payment_method.py`, `test_process_payment.py`, `test_registry.py` | Gateway contract tests, FSM transitions, Celery task idempotency |
| `coupon` | `test_services.py`, `test_validator.py` | Coupon apply/remove/revalidate, all constraint types, concurrent usage-limit race |
| `invoice` | `test_services.py`, `test_tasks.py` | Two-phase PDF generation, idempotent retry paths, Celery task |
| `tenant` | `test_middleware.py`, `test_managers.py`, `test_models.py` | Tenant isolation, middleware error paths, cross-tenant data leak prevention |
| `core` | `test_health_endpoints.py`, `test_responses.py`, `test_request_id_middleware.py`, `test_seed_demo_data.py` | Health/ready probes, RFC 7807 responses, request correlation, seed command |
| `addresses` | `test_services.py` | Address creation |

---

## 5. Known Constraints / Out of Scope

| Item | Notes |
|------|-------|
| Bearer-token auth | API gateway is the enforcement point; views trust `X-User-Id` header as an interim contract (`PROJECT_SPEC.md` §4.3). |
| Full B2B procurement workflow | Approval workflows, net-N payment terms, multi-line shipping splits, and creator/approver roles are aspirational features described in `PROJECT_SPEC.md` §Bonus features under "later iterations." Only snapshot metadata ships in this iteration. |
| Catalog microservice | `Product` is a local model; real catalog integration would use a remote call. |
| Row-level security (RLS) | Tenant isolation is enforced at the application layer via `TenantAwareManager`; Postgres RLS is the roadmap item (`PROJECT_SPEC.md` §9.7). |
| Real payment gateways | Stripe / HyperPay / Tabby are referenced only as integration examples; only deterministic dummy gateways are registered. |
| Address book CRUD | Addresses are created inline via `PUT /api/v1/cart/address/`; a standalone address management API is out of scope. |
| Order listing / status polling | `GET /api/v1/orders/{id}/` is not wired in this iteration. |
| Public invoice retrieval | Invoices are generated asynchronously and accessible via Django admin and `MEDIA_ROOT`; a public `GET /invoices/{id}/` REST endpoint is planned for a future iteration. |

---

## 6. Validation Sequence

Run these commands in order to validate the full stack from startup through endpoint checks:

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

# Set B2B details (canonical RESTful)
curl -s -X PUT $BASE/business-details/ $H \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Acme Corp", "purchase_order_reference": "PO-001"}' \
  | python -m json.tool

# Add a product (canonical RESTful; product UUID from seed_demo_data output)
curl -s -X POST $BASE/items/ $H \
  -H "Content-Type: application/json" \
  -d '{"product_id": "<uuid>", "quantity": 2}' \
  | python -m json.tool

# Checkout (after add-address + add-payment-method)
curl -s -X POST $BASE/checkout/ $H \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  | python -m json.tool
```
