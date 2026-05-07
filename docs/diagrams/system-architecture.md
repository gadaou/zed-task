# System Architecture

```mermaid
flowchart TB
    subgraph clients [Clients]
        Browser["Browser / API Client"]
        Swagger["Swagger UI\n/api/docs/"]
    end

    subgraph django [Django API Process]
        MW["Middleware Stack\nRequestIdMiddleware\nTenantMiddleware"]
        Views["DRF Views\n(thin: parse · validate · dispatch)"]
        Services["Service Layer\nCheckoutService · PaymentService\nInvoiceService · CartService\nCouponService"]
        Health["Health Endpoint\n/api/v1/health/"]
        Schema["OpenAPI Schema\n/api/schema/"]
    end

    subgraph infra [Persistence & Coordination]
        PG[("PostgreSQL 16\nSource of Truth")]
        Redis[("Redis 7\nLocks · Cache · Broker")]
    end

    subgraph async [Async Workers]
        Celery["Celery Worker\nqueue: payments\nqueue: invoices"]
        GatewayRegistry["Payment Gateway Registry\nget_payment_gateway(slug)"]
        DummySuccess["dummy_success gateway"]
        DummyFailing["dummy_failing gateway"]
        DummyTimeout["dummy_timeout gateway"]
        PDFRenderer["Invoice PDF Renderer\nInvoiceService phase 2"]
    end

    subgraph observe [Observability]
        Logging["Structured Logging\nX-Request-Id · X-User-Id\nbound to every log record"]
    end

    Browser --> MW
    Swagger --> MW
    MW --> Views
    Views --> Services
    Services --> PG
    Services --> Redis
    Services -->|"transaction.on_commit"| Redis
    Redis -->|"Celery broker db/1"| Celery
    Celery --> GatewayRegistry
    GatewayRegistry --> DummySuccess
    GatewayRegistry --> DummyFailing
    GatewayRegistry --> DummyTimeout
    Celery --> PDFRenderer
    PDFRenderer --> PG
    Celery --> PG
    Health --> PG
    Health --> Redis
    Schema --> Swagger
    MW --> Logging
    Services --> Logging
    Celery --> Logging
```

- Every request passes through `TenantMiddleware` before reaching a view; tenant context is set once and propagated via a `ContextVar` — no per-query tenant parameter threading required.
- PostgreSQL is the **only system of record**. Redis carries ephemeral coordination data (locks, sentinels, cache) that can be lost and rebuilt without data loss.
- Celery workers share the same application code; the gateway registry is populated at startup via `PaymentConfig.ready()` so worker processes have the same gateway map as the web process.
- Health and schema endpoints are exempt from tenant resolution, allowing load-balancer probes and CI schema export to operate without a tenant header.
