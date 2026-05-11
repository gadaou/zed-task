# Submission Summary

This file is a reviewer-facing executive summary.  
For full details and operational depth, see [README.md](README.md), [RUNBOOK.md](RUNBOOK.md), [FINAL_REVIEW.md](FINAL_REVIEW.md), [docs/architecture.md](docs/architecture.md), [docs/diagrams/](docs/diagrams/), and [docs/final-verification.md](docs/final-verification.md).

## 1. Project Overview

`cart_system` is a multi-tenant cart and checkout service focused on correctness under concurrency and explicit tenant boundaries.  
All tenant data lives in one shared PostgreSQL database, which remains the only source of truth.  
Redis handles checkout locks, idempotency in-progress sentinels, cart cache coordination, rate-limit counters, and Celery brokering.  
Celery workers process payment authorization and invoice generation asynchronously to keep checkout latency predictable.  
The API is built with Django REST Framework and documented through generated OpenAPI plus Swagger/ReDoc endpoints.

## 2. Core Requirements Covered

- [x] **Single database multi-tenancy** - shared-schema design with tenant-scoped models on one PostgreSQL cluster.
- [x] **Tenant isolation** - tenant context resolved from `X-Tenant-Domain` and enforced by tenant-aware ORM scoping.
- [x] **Add/remove product** - `POST /cart/items/` and `DELETE /cart/items/{product_id}/` implemented and tested (legacy action-style routes preserved for backwards compatibility).
- [x] **Add/remove coupon** - `POST /cart/coupons/` and `DELETE /cart/coupons/{coupon_id}/` with validation, stacking policy, and usage tracking.
- [x] **Add address** - `PUT /cart/address/` cart address assignment endpoint implemented and covered by tests.
- [x] **Add payment method** - `PUT /cart/payment-method/` selection endpoint with gateway-slug validation.
- [x] **Checkout** - checkout flow implemented with idempotency, locking, stock deduction, and async payment dispatch.
- [x] **Race-condition handling** - lock + DB transaction + row-level locking + conditional updates protect critical paths.
- [x] **Pluggable payment gateways** - `PaymentGateway` abstraction + registry + deterministic dummy gateways.
- [x] **Documentation and tests** - comprehensive docs plus test suite coverage validated in final report.

## 3. Bonus Requirements Covered

- [x] **Invoice handling** - asynchronous invoice pipeline with persistent invoice records and PDF generation.
- [x] **Coupon constraints** - rule-driven constraints (e.g., min total, country allowlist, usage limits).
- [x] **B2B metadata/orders** - cart business fields snapshotted into orders and included in invoice flow.

## 4. Strongest Reliability Mechanisms

Checkout requires an `Idempotency-Key` HTTP header (client-generated UUID). The server enforces three replay rules: same key + same body returns the stored response without re-executing side-effects; same key + different body returns `409 idempotency/conflict`; same key while the original request is still processing returns `409 idempotency/in-progress`. Durable idempotency records live in PostgreSQL (`IdempotencyRecord`, unique on `(tenant_id, key)`); Redis is used only for in-progress coordination via a `SET NX EX` sentinel — a Redis flush loses no completed replay data. Checkout also runs behind a Redis lock with fenced token-checked release, and critical write paths use `transaction.atomic` plus `select_for_update`.

Inventory correctness is enforced with conditional stock deduction (`WHERE stock >= quantity`) and coupon revalidation immediately before commit. Async dispatch always goes through `transaction.on_commit`, so rolled-back transactions never queue payment or invoice work. Invoice generation is split into an atomic DB phase and a PDF phase outside the transaction to make retries recoverable.

## 5. How to Run Locally

```bash
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo_data
open http://localhost:8000/api/docs/
docker compose exec web pytest -q
```

For full operational commands and troubleshooting paths, see [RUNBOOK.md](RUNBOOK.md).

## 6. Swagger / API Docs

- Swagger UI: <http://localhost:8000/api/docs/>
- OpenAPI schema: <http://localhost:8000/api/schema/>
- ReDoc: <http://localhost:8000/api/redoc/>

## 7. Architecture Diagrams

Architecture and sequence diagrams are in [docs/diagrams/](docs/diagrams/).

Examples included there:
- system architecture
- checkout sequence
- tenant isolation flow
- data model ERD
- payment flow
- invoice flow

For narrative architecture explanation, see [docs/architecture.md](docs/architecture.md).

## 8. Final Verification

Final verification report: [docs/final-verification.md](docs/final-verification.md)

Key outcomes from the report:
- `393 passed, 5 skipped, 0 failed`
- Docker/PostgreSQL/Redis runtime gate passed
- OpenAPI validation passed
- live endpoint smoke tests passed

## 9. Intentional Limitations / Future Improvements

- Dummy payment gateways only in this iteration (no live external provider integration yet).
- `X-User-Id` identity contract is used currently instead of full JWT auth enforcement in-app.
- B2B scope is lightweight metadata/snapshotting, not a full procurement workflow.
- Public invoice download endpoint is not exposed yet.
- Future scale path includes read replicas, sharding/Citus, PostgreSQL RLS hardening, and RabbitMQ/Kafka if scale or queue semantics require it.

