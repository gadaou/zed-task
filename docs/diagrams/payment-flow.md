# Payment Flow

## Gateway Dispatch

```mermaid
flowchart TD
    Checkout["CheckoutService\ncreates Payment\nstatus=REQUIRES_CONFIRMATION\nprovider=gateway_slug"]

    Checkout -->|"transaction.on_commit"| Enqueue["enqueue_authorize_payment\n(Celery: queue=payments)"]

    Enqueue --> Task["authorize_payment task\nload Payment + tenant_context"]
    Task --> Registry["get_payment_gateway(provider)\ngateway registry lookup"]

    Registry --> GW{"Gateway?"}
    GW -->|dummy_success| GS["dummy_success\nalways returns AuthorizeResult.success"]
    GW -->|dummy_failing| GF["dummy_failing\nreturns AuthorizeResult.decline"]
    GW -->|dummy_timeout| GT["dummy_timeout\nraises GatewayTimeout"]
    GW -->|unknown slug| GE["GatewayNotFound\nPayment status=FAILED"]

    GS --> AuthOK["PaymentService\nUPDATE Payment\nstatus=AUTHORIZED\ngateway_authorization_id"]
    AuthOK --> OrderPaid["UPDATE Order\nstatus=PAID"]
    OrderPaid -->|"on_commit"| InvoiceTask["enqueue_generate_invoice"]

    GF --> AuthFail["PaymentService\nUPDATE Payment\nstatus=FAILED\nfailure_reason"]
    AuthFail --> OrderFail["UPDATE Order\nstatus=FAILED"]

    GT --> Retry{"Max retries\nreached?"}
    Retry -->|no| Backoff["Celery retry\ncountdown backoff\n(GatewayTimeout / GatewayUnavailable)"]
    Backoff --> Task
    Retry -->|yes| AuthFail
```

## Payment Status FSM

```mermaid
stateDiagram-v2
    [*] --> REQUIRES_CONFIRMATION : CheckoutService creates Payment

    REQUIRES_CONFIRMATION --> AUTHORIZED : gateway.authorize_payment() success
    REQUIRES_CONFIRMATION --> FAILED : gateway decline or max retries

    AUTHORIZED --> CAPTURED : gateway.capture_payment()
    AUTHORIZED --> CANCELLED : void before capture

    CAPTURED --> SUCCEEDED : settlement confirmed
    CAPTURED --> REFUNDED : refund issued post-capture

    FAILED --> [*]
    CANCELLED --> [*]
    SUCCEEDED --> [*]
    REFUNDED --> [*]
```

- `CheckoutService` creates the `Payment` row **inside** the database transaction; the Celery task is only enqueued after commit via `transaction.on_commit`, so no gateway call fires for a rolled-back checkout.
- `get_payment_gateway(slug)` is the **sole coupling point** between `PaymentService` and any gateway implementation; adding a real gateway requires registering a class — no changes to `PaymentService`.
- The `dummy_timeout` gateway and `GatewayUnavailable` exception trigger **Celery retries with countdown backoff**, making the payment pipeline resilient to transient gateway degradation without blocking the web process.
- Status transitions are enforced at the application layer in `PaymentService`; the `ck_payment_status_valid` DB CHECK is a belt-and-suspenders guard against ORM bypass.
