# Observability Guide

This document describes the observability foundations in `cart_system`: request correlation, structured logging, lifecycle events, and the production metrics worth tracking.

The design is **vendor-neutral**: no monitoring client is bundled. Observability data flows through the Python logging pipeline, so routing to Datadog, Prometheus, OpenTelemetry, CloudWatch, Loki, or another backend is a deployment choice.

---

## 1. Request Correlation (`X-Request-Id`)

Every HTTP request carries a stable correlation identifier throughout its lifetime.

**How it works**

- `RequestIdMiddleware` (`apps.core.middleware`) runs as the second middleware (after `SecurityMiddleware`, before `TenantMiddleware`).
- If the client sends an `X-Request-Id` header, that value is preserved (after a safety sanity check: max 128 characters, non-empty after strip).
- If the header is absent or invalid, a new UUID4 hex string is generated.
- The resolved id is:
  - Stored on `request.request_id` for direct access in views.
  - Bound into the `request_id` `ContextVar` (`apps.core.context`) so every log record emitted downstream — in service layer, Celery task preambles, etc. — carries it without explicit argument passing.
  - Echoed back in the `X-Request-Id` **response header** so clients can correlate their request with server-side log lines.

**Generating a correlation id client-side**

```http
POST /api/v1/cart/checkout/ HTTP/1.1
X-Tenant-Domain: acme.mysaas.com
X-User-Id: 11111111-1111-1111-1111-111111111111
X-Request-Id: 7b2e9c41-4e8d-4b0e-beed-5f3a2c87f1e2
Idempotency-Key: 9a8b7c6d-5e4f-3a2b-1c0d-ef1234567890
Content-Type: application/json
```

If you omit `X-Request-Id`, the server generates one and includes it in the response. Log aggregation pipelines can then join on this field across multiple services.

---

## 2. Structured Logging

### Log format

**Development / test** (`VerboseTextFormatter`, default in `base.py`)

```
[2026-05-07 02:30:00] INFO     apps.order.services req=7b2e9c41 tenant=aaa user=111 checkout.completed outcome=success cart_id=ccc order_id=ddd duration_ms=87
```

**Production** (`JsonFormatter`, active when `LOGGING["handlers"]["console"]["formatter"] = "json"` in `prod.py`)

```json
{
  "ts": "2026-05-07T02:30:00.123456",
  "level": "INFO",
  "logger": "apps.order.services",
  "msg": "checkout.completed",
  "request_id": "7b2e9c41...",
  "tenant_id": "aaa...",
  "user_id": "111...",
  "action": "checkout.completed",
  "outcome": "success",
  "cart_id": "ccc...",
  "order_id": "ddd...",
  "payment_id": "eee...",
  "duration_ms": 87
}
```

The `RequestContextFilter` injects `request_id`, `tenant_id`, and `user_id` onto every `LogRecord`. Service code attaches domain-specific fields via `extra={}` or the `log_event` helper.

### Standard log fields

| Field | Source | Description |
|---|---|---|
| `request_id` | `RequestContextFilter` / `RequestIdMiddleware` | Request correlation id |
| `tenant_id` | `RequestContextFilter` / `TenantMiddleware` | Active tenant UUID |
| `user_id` | `RequestContextFilter` / `X-User-Id` header | Customer UUID (if present) |
| `action` | service code `extra={"action": ...}` | Machine-readable event name |
| `outcome` | service code | `success` / `failed` / `timeout` / `skipped` / `replay` |
| `duration_ms` | service code | Wall-clock time of the instrumented block |
| `cart_id` | service code | UUID of the relevant cart |
| `order_id` | service code | UUID of the relevant order |
| `payment_id` | service code | UUID of the relevant payment |
| `invoice_id` | service code | UUID of the relevant invoice |
| `provider` | payment service | Gateway slug (e.g. `stripe`, `dummy_success`) |
| `reason` | service code | Error code or exception class name |
| `component` | readiness view | Dependency name (`postgres` / `redis`) |
| `metric` | `apps.core.metrics.incr` | Metric name (on `metric.incr` records) |

### Emitting lifecycle logs in service code

```python
from apps.core.logging import log_event
import logging

logger = logging.getLogger(__name__)

# Simple lifecycle event
log_event(logger, "checkout.completed",
          cart_id=str(cart_id), order_id=str(order_id), duration_ms=elapsed)

# Failure event
log_event(logger, "payment.declined", level=logging.WARNING,
          outcome="declined", payment_id=str(pid), reason=error_code)
```

---

## 3. Lifecycle Event Catalog

All events use the `action` field for log-query filtering (for example, `action:"checkout.completed"` in Datadog).

### Checkout

| `action` | Level | When emitted |
|---|---|---|
| `checkout.started` | INFO | Entry point of `CheckoutService.checkout` |
| `checkout.completed` | INFO | Checkout succeeded; includes `order_id`, `payment_id`, `duration_ms` |
| `checkout.failed` | ERROR | Any domain/lock error during checkout; includes `reason` |
| `checkout.lock_failed` | WARNING | Redis lock already held; includes `lock_key` |
| `checkout.transaction_committed` | INFO | DB transaction committed successfully |

### Payment

| `action` | Level | When emitted |
|---|---|---|
| `payment.authorize_started` | INFO | Gateway call is about to be issued |
| `payment.authorized` | INFO | Gateway returned success (`outcome=success`) or idempotent skip (`outcome=skipped`) |
| `payment.declined` | WARNING | Gateway returned a hard decline |
| `payment.timeout` | WARNING | `GatewayTimeout` / `GatewayUnavailable` raised by gateway |

### Idempotency

| `action` | Level | When emitted |
|---|---|---|
| `idempotency.replay` | INFO | Same key + same hash seen again; stored response returned |
| `idempotency.conflict` | WARNING | Same key, different request payload |
| `idempotency.in_progress` | INFO | Concurrent request holds the idempotency sentinel |

### Cart mutations

| `action` | Level | When emitted |
|---|---|---|
| `cart.add_product` | INFO / ERROR | Product added to cart (outcome=success/failed) |
| `cart.remove_product` | INFO / ERROR | Product removed from cart |
| `cart.set_address` | INFO / ERROR | Shipping address selected |
| `cart.set_payment_method` | INFO / ERROR | Payment method selected |

### Invoice

| `action` | Level | When emitted |
|---|---|---|
| `invoice.generated` | INFO | Invoice fully generated (`outcome=success`) or skipped (`outcome=skipped`) |
| `invoice.pdf_retry` | INFO | PDF render retried (DB row existed but `pdf_url` was empty) |
| `invoice.concurrent_insert` | INFO | Concurrent task won the INSERT race |
| `invoice.task_completed` | INFO | Celery task finished successfully |
| `invoice.failed` | ERROR | Invoice generation failed after retries |

### Readiness

| `action` | Level | When emitted |
|---|---|---|
| `readiness.dependency_failed` | ERROR | A dependency check (`postgres` / `redis`) returned unhealthy |

---

## 4. Lightweight Metric Hooks

`apps.core.metrics.incr(name, **labels)` emits metric events as structured log records (level INFO, logger `apps.core.metrics`, msg `metric.incr`). These are queryable from your log aggregator without any additional client.

### Metric names emitted

| Metric name | Labels | What it measures |
|---|---|---|
| `checkout.failed` | `reason`, `tenant_id` | Checkout aborted with a domain or lock error |
| `checkout.lock_contention` | `tenant_id` | Redis lock already held at checkout entry |
| `payment.authorized` | `provider` | Gateway returned success |
| `payment.declined` | `provider`, `reason` | Gateway returned a hard decline |
| `payment.timeout` | `provider` | Gateway timed out or was unavailable |
| `idempotency.replay` | `tenant_id` | Identical key+hash replay returned stored response |
| `idempotency.conflict` | `tenant_id` | Same key, different request payload |
| `idempotency.in_progress` | `tenant_id` | Concurrent request holds the sentinel |
| `readiness.dependency_failed` | `component` | A readiness check failed (`postgres` or `redis`) |
| `invoice.failed` | `reason` | Invoice generation failed after retries |

---

## 5. Recommended Production Metrics

These are the key operational metrics to track in production. Derived from the `metric.incr` log records above plus data available in the application and infra layers.

### Checkout

| Metric | Recommended alert threshold |
|---|---|
| **Checkout latency p50 / p95 / p99** — derived from `duration_ms` on `checkout.completed` records | p99 > 5 s → investigate gateway or lock contention |
| **Checkout failure rate by reason** — `checkout.failed` count grouped by `reason` label | > 1% failure rate → alert |
| **Redis lock contention rate** — `checkout.lock_contention` count / `checkout.started` count | > 5% → horizontally scale checkout workers |

### Payment

| Metric | Recommended alert threshold |
|---|---|
| **Payment authorization success rate** — `payment.authorized` (outcome=success) / total authorizations | < 95% → investigate gateway |
| **Payment decline rate** — `payment.declined` count | Sudden spike → potential fraud or gateway misconfiguration |
| **Payment timeout rate** — `payment.timeout` / total authorizations | > 2% → gateway latency issue; review retry back-off |

### Idempotency

| Metric | Recommended alert threshold |
|---|---|
| **Idempotency replay rate** — `idempotency.replay` / checkout attempts | Healthy baseline; sudden spike may indicate client retry storms |
| **Idempotency conflict rate** — `idempotency.conflict` / checkout attempts | > 0.1% → client bug (reusing keys with different payloads) |

### Cart cache

| Metric | How to derive |
|---|---|
| **Cart cache hit rate** | Log `cache.hit` / `cache.miss` events from `apps.core.cache` — add `log_event` calls there to surface hit/miss in the log pipeline |
| **Cache miss rate spike** | > 30% miss on a stable baseline → Redis eviction or TTL misconfiguration |

### Invoice

| Metric | Recommended alert threshold |
|---|---|
| **Invoice generation success rate** | < 99% → investigate PDF rendering or storage |
| **Invoice failure rate** — `invoice.failed` count | Any failures during business hours → alert |

### Infrastructure

| Metric | Source |
|---|---|
| **DB query latency (p50/p95/p99)** | PostgreSQL `pg_stat_statements` or Datadog APM |
| **DB connection pool saturation** | `psycopg` pool metrics or PgBouncer stats |
| **Celery queue depth** — `payments` and `invoices` queues | Celery Flower, Redis `LLEN`, or `celery inspect` |
| **Celery task retry rate** — retried tasks / total tasks | Persistent retries → gateway degradation |
| **Redis memory usage** | `redis_memory_used_bytes` from Redis INFO |
| **Redis command latency** | Redis slowlog + `latency_histogram` |

---

## 6. Future Vendor Integration

The observability stack is designed to be swapped without touching service code.

### Datadog

1. Ship JSON logs to a Datadog agent (set `DD_LOGS_ENABLED=true`; configure log forwarding from stdout).
2. Create log facets for `action`, `request_id`, `tenant_id`, `duration_ms`.
3. Build monitors on `action:checkout.failed` count and `duration_ms` percentiles.
4. For APM tracing: add `ddtrace` and wrap `RequestIdMiddleware` or replace the `X-Request-Id` with `dd-trace-id`.

### Prometheus

1. Replace `apps/core/metrics.py::incr` with `prometheus_client.Counter.labels(...).inc()`.
2. Mount a `MetricsView` at `/metrics` (or behind an internal network).
3. Use Alertmanager rules on `checkout_failed_total` and `payment_timeout_total`.

### OpenTelemetry

1. Add `opentelemetry-sdk` and `opentelemetry-instrumentation-django`.
2. Place the OTel Django middleware before `RequestIdMiddleware` so trace context (`traceparent`) is resolved first.
3. Forward the OTel `trace_id` as the `X-Request-Id` (or correlate both in logs).
4. Ship logs, traces, and metrics to the OTel Collector; from there to any compatible backend.

In all three cases, `apps/core/context.py`, `apps/core/logging.py`, and the service-layer `log_event` calls remain unchanged.
