# Cache, Idempotency, and Locks

```mermaid
flowchart LR
    subgraph CartCache ["Cart Read Cache\ncart:read:{tenant_id}:{user_id}"]
        direction TB
        CGet["GET cart request"]
        CHit{"Cache hit?"}
        CMiss["DB read\nCart + CartItems\n+ applied coupons"]
        CSet["SET EX {TTL}\n(CART_CACHE_TTL)"]
        CReturn["Return cached\ncart JSON"]
        CMutate["Cart mutation\n(add/remove item,\napply coupon,\nset address...)"]
        COnCommit["schedule_cart_cache_invalidation\n(transaction.on_commit)"]
        CDel["DEL cart:read:{t}:{u}"]

        CGet --> CHit
        CHit -->|hit| CReturn
        CHit -->|miss| CMiss
        CMiss --> CSet
        CSet --> CReturn
        CMutate --> COnCommit
        COnCommit --> CDel
    end

    subgraph IdempotencyLayer ["Idempotency\nidem:{tenant_id}:{key}"]
        direction TB
        IReq["POST /cart/checkout/\nIdempotency-Key header"]
        IDBCheck["SELECT IdempotencyRecord\nWHERE (tenant_id, key)"]
        IReplay{"DB record\nexists?"}
        IReplayOK["Return stored 202\n(same hash — replay)"]
        IConflict["409 Conflict\n(different hash)"]
        IRedisNX["SET NX EX\nidem:{t}:{k} = in_progress"]
        IInFlight{"Redis key\nexisted?"}
        I503["503 / wait\n(concurrent in-flight request)"]
        IProcess["Process checkout"]
        IDBInsert["INSERT IdempotencyRecord\n(inside transaction.atomic)\nstatus=SUCCEEDED + response"]
        IClearSentinel["DEL idem:{t}:{k}\n(finally block)"]

        IReq --> IDBCheck
        IDBCheck --> IReplay
        IReplay -->|same hash| IReplayOK
        IReplay -->|diff hash| IConflict
        IReplay -->|no record| IRedisNX
        IRedisNX --> IInFlight
        IInFlight -->|key existed| I503
        IInFlight -->|acquired| IProcess
        IProcess --> IDBInsert
        IDBInsert --> IClearSentinel
    end

    subgraph CheckoutLock ["Checkout Lock\nlock:checkout:{tenant_id}:{cart_id}"]
        direction TB
        LAcquire["SET NX PX\nlock:checkout:{t}:{c} token TTL"]
        LCheck{"Acquired?"}
        LBusy["423 Locked\n(cart already being\nchecked out)"]
        LHeld["Proceed inside\ntransaction.atomic()"]
        LRelease["Lua script:\nif GET key == token\n  then DEL key\n(fenced release)"]

        LAcquire --> LCheck
        LCheck -->|no| LBusy
        LCheck -->|yes| LHeld
        LHeld --> LRelease
    end
```

- The **cart read cache** (`cart:read:{t}:{u}`) is invalidated via `transaction.on_commit`, so a cache entry is never deleted before the mutating transaction is durable — readers never see stale data from an uncommitted write.
- The **idempotency fast path** uses a Redis `SET NX EX` sentinel to detect a concurrent in-flight request for the same key before hitting the database; the durable `IdempotencyRecord` in PostgreSQL handles replay and conflict detection for completed requests across process restarts.
- The `IdempotencyRecord` is inserted **inside** `transaction.atomic()` so a rollback removes the record atomically — there are no "succeeded" records pointing at orders that were never committed.
- The **checkout lock** uses a Lua-fenced release (`GET + DEL if token matches`) to ensure that only the process that acquired the lock can release it, preventing a slow worker from unlocking a lock re-acquired by a newer request.
