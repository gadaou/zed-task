# Submission Summary

This file is a reviewer-facing executive summary.  
For full details and operational depth, see [README.md](README.md), [RUNBOOK.md](RUNBOOK.md), [FINAL_REVIEW.md](FINAL_REVIEW.md), [docs/architecture.md](docs/architecture.md), [docs/diagrams/](docs/diagrams/), and [docs/final-verification.md](docs/final-verification.md).

## 1. Project Overview

`cart_system` is a multi-tenant cart and checkout service designed around correctness under concurrency and clear tenant boundaries.  
All tenant data lives in a single shared PostgreSQL database, which is the only source of truth.  
Redis is used for distributed checkout locks, idempotency in-progress sentinels, cart cache coordination, rate limiting counters, and Celery brokering.  
Celery workers handle asynchronous payment authorization and invoice generation so checkout remains responsive.  
The API is implemented with Django REST Framework, with OpenAPI generation and interactive Swagger/ReDoc docs.

## 2. Core Requirements Covered

- [x] **Single database multi-tenancy** - shared-schema design with tenant-scoped models on one PostgreSQL cluster.
- [x] **Tenant isolation** - tenant context resolved from `X-Tenant-Domain` and enforced by tenant-aware ORM scoping.
- [x] **Add/remove product** - `add-product` and `remove-product` cart endpoints implemented and tested.
- [x] **Add/remove coupon** - apply/remove coupon endpoints with validation, stacking policy, and usage tracking.
- [x] **Add address** - cart address assignment endpoint implemented and covered by tests.
- [x] **Add payment method** - payment-method selection endpoint with gateway-slug validation.
- [x] **Checkout** - checkout flow implemented with idempotency, locking, stock deduction, and async payment dispatch.
- [x] **Race-condition handling** - lock + DB transaction + row-level locking + conditional updates protect critical paths.
- [x] **Pluggable payment gateways** - `PaymentGateway` abstraction + registry + deterministic dummy gateways.
- [x] **Documentation and tests** - comprehensive docs plus test suite coverage validated in final report.

## 3. Bonus Requirements Covered

- [x] **Invoice handling** - asynchronous invoice pipeline with persistent invoice records and PDF generation.
- [x] **Coupon constraints** - rule-driven constraints (e.g., min total, country allowlist, usage limits).
- [x] **B2B metadata/orders** - cart business fields snapshotted into orders and included in invoice flow.

## 4. Strongest Reliability Mechanisms

- Durable idempotency record in PostgreSQL plus Redis in-progress sentinel for safe retry/replay behavior.
- Redis checkout lock with fenced token-checked release to avoid stale unlock hazards.
- `transaction.atomic` with `select_for_update` on critical rows in checkout/payment/invoice paths.
- Conditional stock deduction (`WHERE stock >= quantity`) to prevent overselling.
- Coupon revalidation at checkout-time before order commit.
- `transaction.on_commit` for Celery dispatch so rolled-back transactions do not enqueue async work.
- Two-phase invoice generation (atomic DB phase, PDF phase outside transaction) for recoverable retries.

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
- `360 passed, 5 skipped, 0 failed`
- Docker/PostgreSQL/Redis runtime gate passed
- OpenAPI validation passed
- live endpoint smoke tests passed

## 9. Intentional Limitations / Future Improvements

- Dummy payment gateways only in this iteration (no live external provider integration yet).
- `X-User-Id` identity contract is used currently instead of full JWT auth enforcement in-app.
- B2B scope is lightweight metadata/snapshotting, not a full procurement workflow.
- Public invoice download endpoint is not exposed yet.
- Future scale path includes read replicas, sharding/Citus, PostgreSQL RLS hardening, and RabbitMQ/Kafka if scale or queue semantics require it.

