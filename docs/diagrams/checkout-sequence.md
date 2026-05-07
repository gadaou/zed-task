# Checkout Sequence

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant TenantMW as TenantMiddleware
    participant View as CheckoutView
    participant IdemMgr as IdempotencyManager
    participant Lock as RedisLock
    participant Redis
    participant DB as PostgreSQL
    participant Coupon as CouponService
    participant Celery

    Client->>TenantMW: POST /api/v1/cart/checkout/<br/>X-Tenant-Domain · X-User-Id · Idempotency-Key
    TenantMW->>DB: SELECT tenant WHERE domain=X-Tenant-Domain
    DB-->>TenantMW: Tenant row
    TenantMW->>TenantMW: set ContextVar(tenant)
    TenantMW->>View: request.tenant attached

    View->>IdemMgr: check_for_replay(tenant, key, hash)
    IdemMgr->>DB: SELECT IdempotencyRecord WHERE (tenant_id, key)
    DB-->>IdemMgr: row or None
    alt existing record — same hash
        IdemMgr-->>View: replay stored 202 response
        View-->>Client: 202 (replayed)
    else existing record — different hash
        IdemMgr-->>View: raise IdempotencyConflict
        View-->>Client: 409 Conflict
    end

    IdemMgr->>Redis: SET NX EX idem:{tenant}:{key} "in_progress"
    note over Redis: in-flight sentinel

    View->>Lock: redis_lock(lock:checkout:{tenant}:{cart})
    Lock->>Redis: SET NX PX lock:checkout:{tenant}:{cart} token
    Redis-->>Lock: OK (acquired)

    View->>DB: BEGIN transaction.atomic()
    View->>DB: SELECT cart FOR UPDATE (version check)
    View->>DB: SELECT address FOR UPDATE
    View->>DB: SELECT payment_method FOR UPDATE

    View->>Coupon: revalidate_cart_coupons(cart)
    Coupon->>DB: re-check each coupon constraint
    Coupon-->>View: validated discounts

    View->>DB: UPDATE product SET stock=stock-qty<br/>WHERE stock >= qty (conditional, per item)
    note over DB: raises InsufficientStock if 0 rows affected

    View->>DB: INSERT Order (status=PENDING_PAYMENT,<br/>B2B snapshot, money snapshot, idempotency_key)
    View->>DB: INSERT OrderItems (price snapshots)
    View->>DB: UPDATE cart SET status=CHECKED_OUT, version=version+1
    View->>DB: INSERT Payment (status=REQUIRES_CONFIRMATION,<br/>provider=gateway_slug, amount)
    View->>DB: INSERT IdempotencyRecord (status=SUCCEEDED, 202 body)
    View->>DB: schedule_cart_cache_invalidation (on_commit)
    View->>DB: enqueue_authorize_payment (on_commit)
    View->>DB: COMMIT

    DB-->>View: commit OK
    View-->>Lock: release_lock (Lua fenced)
    Lock->>Redis: DEL lock:checkout:{tenant}:{cart} (if token matches)
    View->>Redis: DEL idem:{tenant}:{key}  (sentinel cleared)
    View-->>Client: 202 Accepted {payment_status: "pending"}

    note over Celery: after transaction commits
    DB-->>Celery: enqueue authorize_payment task (broker)
    Celery->>DB: load Payment + Order (cross-tenant safe)
    Celery->>Celery: get_payment_gateway(provider)
    Celery->>Celery: gateway.authorize_payment(...)
    alt gateway success
        Celery->>DB: UPDATE Payment status=AUTHORIZED
        Celery->>DB: UPDATE Order status=PAID
        Celery->>DB: enqueue_generate_invoice (on_commit)
    else gateway decline
        Celery->>DB: UPDATE Payment status=FAILED, failure_reason
        Celery->>DB: UPDATE Order status=FAILED
    else GatewayTimeout
        Celery->>Celery: retry with countdown backoff
    end
```

- The **idempotency check happens before any lock is acquired**: a replayed request returns the stored 202 without touching the database, the stock table, or the gateway.
- The **Redis checkout lock** (`SET NX PX`) prevents two concurrent requests for the same cart from entering `transaction.atomic` simultaneously; the Lua-fenced release ensures only the lock owner can release it.
- **`select_for_update`** on cart, address, and payment_method inside the transaction prevents phantom reads and ensures the checkout sees a consistent, mutation-free view of all three resources until commit.
- **`transaction.on_commit`** guarantees that the Celery task is only enqueued after the database transaction has durably committed — no payment task fires for a cart that was never successfully checked out.
