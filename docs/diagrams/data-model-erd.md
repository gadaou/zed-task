# Data Model — Entity Relationship Diagram

```mermaid
erDiagram
    Tenant {
        UUID id PK
        string name
        string domain UK
        bool is_active
        datetime created_at
        datetime updated_at
    }

    Product {
        UUID id PK
        UUID tenant_id FK
        string name
        decimal price
        string currency
        int stock
        datetime created_at
        datetime updated_at
    }

    Address {
        UUID id PK
        UUID tenant_id FK
        UUID user_id
        string country
        string city
        text details
        string label
        bool is_default
        datetime deleted_at
        datetime created_at
        datetime updated_at
    }

    PaymentMethod {
        UUID id PK
        UUID tenant_id FK
        string gateway_slug
        datetime created_at
        datetime updated_at
    }

    Cart {
        UUID id PK
        UUID tenant_id FK
        UUID user_id
        string status
        decimal total_price
        decimal discount_amount
        decimal total_after_discount
        string currency
        int version
        UUID selected_address_id FK
        UUID selected_payment_method_id FK
        string company_name
        string tax_number
        string purchase_order_reference
        datetime created_at
        datetime updated_at
    }

    CartItem {
        UUID id PK
        UUID tenant_id FK
        UUID cart_id FK
        UUID product_id
        int quantity
        decimal price_snapshot
        string currency
        datetime created_at
        datetime updated_at
    }

    Coupon {
        UUID id PK
        UUID tenant_id FK
        string code
        string discount_type
        decimal value
        string currency
        json constraints
        int usage_limit
        int used_count
        bool is_active
        datetime starts_at
        datetime ends_at
        datetime created_at
        datetime updated_at
    }

    CartCoupon {
        UUID id PK
        UUID tenant_id FK
        UUID cart_id FK
        UUID coupon_id FK
        decimal discount_amount
        string currency
        datetime applied_at
        datetime updated_at
    }

    Order {
        UUID id PK
        UUID tenant_id FK
        UUID user_id
        UUID cart_id FK
        UUID address_id FK
        UUID payment_method_id FK
        string status
        decimal subtotal
        decimal discount_amount
        decimal total
        string currency
        string company_name
        string tax_number
        string purchase_order_reference
        UUID idempotency_key
        int version
        datetime created_at
        datetime updated_at
    }

    OrderItem {
        UUID id PK
        UUID tenant_id FK
        UUID order_id FK
        UUID product_id
        int quantity
        decimal unit_price
        string currency
        datetime created_at
        datetime updated_at
    }

    Payment {
        UUID id PK
        UUID tenant_id FK
        UUID cart_id FK
        string provider
        string status
        decimal amount
        string currency
        string failure_reason
        string gateway_authorization_id
        string gateway_capture_id
        datetime created_at
        datetime updated_at
    }

    InvoiceSequence {
        UUID id PK
        UUID tenant_id FK
        int last_number
        datetime created_at
        datetime updated_at
    }

    Invoice {
        UUID id PK
        UUID tenant_id FK
        UUID order_id FK
        int number
        decimal total
        decimal taxes
        string currency
        string pdf_url
        datetime generated_at
        datetime updated_at
    }

    IdempotencyRecord {
        UUID id PK
        UUID tenant_id
        UUID key
        string request_hash
        string status
        int response_status
        json response_body
        datetime created_at
        datetime updated_at
    }

    Tenant ||--o{ Product : "owns"
    Tenant ||--o{ Address : "owns"
    Tenant ||--o{ PaymentMethod : "owns"
    Tenant ||--o{ Cart : "owns"
    Tenant ||--o{ Coupon : "owns"
    Tenant ||--o{ Order : "owns"
    Tenant ||--o{ Payment : "owns"
    Tenant ||--o{ Invoice : "owns"
    Tenant ||--o{ InvoiceSequence : "has one counter"

    Cart }o--o| Address : "selected_address"
    Cart }o--o| PaymentMethod : "selected_payment_method"
    Cart ||--o{ CartItem : "contains"
    Cart ||--o{ CartCoupon : "has applied"
    Cart ||--o{ Payment : "attempted via"
    Cart ||--o| Order : "checked out as"

    CartItem }o--|| Product : "snapshots price of"
    CartCoupon }o--|| Coupon : "applies"

    Order ||--o{ OrderItem : "contains"
    Order ||--|| Invoice : "generates"
    Order }o--|| Address : "shipped to"
    Order }o--|| PaymentMethod : "paid with"

    OrderItem }o--|| Product : "snapshot of"
```

> **Note:** `IdempotencyRecord.tenant_id` is a plain `UUIDField`, not a FK to `Tenant`. This is intentional — it allows the idempotency sweep job to operate without a full tenant context.

- Every domain model (except `IdempotencyRecord`) inherits from `TenantAwareModel`, which supplies `tenant_id FK`, `created_at`, and `updated_at`. The `TenantAwareManager` auto-filters all queries by the active tenant `ContextVar`.
- `CartItem.product_id` and `OrderItem.product_id` are bare `UUIDField`s (no FK to `Product`). Price snapshots decouple the cart and order aggregates from catalog mutations — a product can be deleted without orphaning historical order lines.
- `Payment.provider` is a free-text gateway slug, not a FK, so new gateways can be registered in code without a schema migration.
- `Invoice` has a `OneToOneField(order)` enforced at the DB level — at most one invoice per order, making duplicate invoice generation safe under at-least-once Celery delivery.
