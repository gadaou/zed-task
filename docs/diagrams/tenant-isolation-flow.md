# Tenant Isolation Flow

```mermaid
flowchart TD
    Request["Incoming HTTP Request"]

    Request --> ExemptCheck{"Exempt path?\n/api/health /api/schema\n/admin ..."}
    ExemptCheck -->|yes| PassThrough["Pass to view\nno tenant context"]
    ExemptCheck -->|no| HeaderCheck{"X-Tenant-Domain\nheader present?"}

    HeaderCheck -->|missing| E400["400 Bad Request\ntenant-domain-required"]
    HeaderCheck -->|present| DBLookup["SELECT tenant\nWHERE domain = header\nAND is_active IN (True, False)"]

    DBLookup --> Found{"Tenant found?"}
    Found -->|not found| E404["404 Not Found\ntenant-not-found"]
    Found -->|found| ActiveCheck{"is_active?"}

    ActiveCheck -->|false| E403["403 Forbidden\ntenant-inactive"]
    ActiveCheck -->|true| SetContext["request.tenant = tenant\nset ContextVar(current_tenant)"]

    SetContext --> View["DRF View\nService Layer"]

    View --> ORM["TenantAwareManager\n.get_queryset()\nauto-filters WHERE tenant_id = ctx.tenant_id"]

    ORM --> ScopeCheck{"Resource\ntenant_id matches\ncontext tenant?"}
    ScopeCheck -->|yes| Data["Return data"]
    ScopeCheck -->|"no (ORM never returns it)"| E404b["Implicit 404\nzero rows - DoesNotExist"]

    ORM --> LockKey["Redis lock key\nlock:checkout:{tenant_id}:{cart_id}"]
    ORM --> CacheKey["Cart cache key\ncart:read:{tenant_id}:{user_id}"]
    ORM --> IdemKey["Idempotency key\nidem:{tenant_id}:{key}"]
```

- `TenantMiddleware` is the **only place** where `X-Tenant-Domain` is resolved to a `Tenant` row; downstream code reads from the `ContextVar` and never re-queries the tenant table.
- `TenantAwareManager` is the **default ORM manager** on every domain model. Its `get_queryset()` unconditionally appends `WHERE tenant_id = <context>`, making a cross-tenant row **invisible**, not just forbidden.
- Every Redis key is **namespaced by `tenant_id`** as the first segment, ensuring that lock contention, cache entries, and idempotency sentinels are fully isolated between tenants even though they share one Redis cluster.
- A request for a resource belonging to a foreign tenant receives a `404`, not a `403` — the resource is treated as if it does not exist, preventing tenant enumeration.
