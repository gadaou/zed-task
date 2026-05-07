# Data Model — Entity Relationship Diagram

```mermaid
erDiagram
    Tenant {
        UUID id PK
        string name
        string domain UK
        bool is_active
    }

    Product {
        UUID id PK
        UUID tenant_id FK
        string name
        decimal price
        string currency
        int stock
    }

    Address {
        UUID id PK
        UUID tenant_id FK
        UUID user_id
        string country
        string city
        bool is_default
        datetime deleted_at
    }

    PaymentMethod {
        UUID id PK
        UUID tenant_id FK
        string gateway_slug
    }

    Cart {
        UUID id PK
        UUID tenant_id FK
        UUID user_id "UNIQUE WHERE status=ACTIVE"
        string status
        decimal total_price
        decimal discount_amount
        decimal total_after_discount
        string currency
        int version
        UUID selected_address_id FK
        UUID selected_payment_method_id FK
    }

    CartItem {
        UUID id PK
        UUID tenant_id FK
        UUID cart_id FK
        UUID product_id "snapshot — no FK"
        int quantity
        decimal price_snapshot "captured at add-time"
        string currency
    }

    Coupon {
        UUID id PK
        UUID tenant_id FK
        string code
        string discount_type
        decimal value
        string currency
        bool is_active
        datetime starts_at
        datetime ends_at
    }

    CartCoupon {
        UUID id PK
        UUID tenant_id FK
        UUID cart_id FK
        UUID coupon_id FK
        decimal discount_amount "snapshot at apply-time"
        string currency
        datetime applied_at
    }

    Payment {
        UUID id PK
        UUID tenant_id FK
        UUID cart_id FK
        string provider
        string status
        decimal amount
        string currency
    }

    Order {
        UUID id PK
        UUID tenant_id FK
        UUID user_id
        UUID cart_id FK
        UUID address_id FK "selected at checkout"
        UUID payment_method_id FK "selected at checkout"
        string status
        decimal subtotal
        decimal discount_amount
        decimal total
        string currency
        UUID idempotency_key
    }

    OrderItem {
        UUID id PK
        UUID tenant_id FK
        UUID order_id FK
        UUID product_id "snapshot — no FK"
        int quantity
        decimal unit_price "snapshot at checkout"
        string currency
    }

    Invoice {
        UUID id PK
        UUID tenant_id FK
        UUID order_id FK
        int number
        decimal total
        decimal taxes
        string currency
        datetime generated_at
    }

    InvoiceSequence {
        int id PK
        UUID tenant_id FK "UNIQUE per tenant"
        int last_number
    }

    IdempotencyRecord {
        UUID id PK
        UUID tenant_id "no FK — intentional"
        UUID key "UNIQUE with tenant_id"
        string request_hash
        string status
        int response_status
        json response_body
    }

    Tenant ||--o{ Product : "owns"
    Tenant ||--o{ Address : "owns"
    Tenant ||--o{ PaymentMethod : "owns"
    Tenant ||--o{ Cart : "owns"
    Tenant ||--o{ Coupon : "owns"
    Tenant ||--o{ Order : "owns"
    Tenant ||--o{ Payment : "owns"
    Tenant ||--o{ Invoice : "owns"
    Tenant ||--|| InvoiceSequence : "has one counter"

    Cart }o--o| Address : "selected_address (nullable)"
    Cart }o--o| PaymentMethod : "selected_payment_method (nullable)"
    Cart ||--o{ CartItem : "contains"
    Cart ||--o{ CartCoupon : "has applied"
    Cart ||--o{ Payment : "attempted via"
    Cart ||--o{ Order : "checked out as"

    CartCoupon }o--|| Coupon : "snapshots discount of"

    Order ||--o{ OrderItem : "contains"
    Order ||--|| Invoice : "generates"
    Order }o--|| Address : "selected at checkout"
    Order }o--|| PaymentMethod : "selected at checkout"
```

## Notes

- **PostgreSQL is the source of truth.** All uniqueness and validity rules are enforced at the database layer via `UniqueConstraint` and `CheckConstraint`, not only by ORM validation.
- **Tenant isolation** is enforced through `tenant_id` on every domain model and through `TenantAwareManager`, which auto-filters all queries by the active tenant. Cross-tenant data access is structurally prevented.
- **B2B fields** (`company_name`, `tax_number`, `purchase_order_reference`) exist on both `Cart` and `Order` but are omitted from the diagram to reduce noise. They are optional for B2C flows (stored as empty strings) and snapshotted onto `Order` at checkout. See [`b2b-flow.md`](b2b-flow.md) for the full flow.
- **Order and invoice data is snapshotted for auditability.** `OrderItem.product_id` is a bare `UUIDField` (no FK) and `OrderItem.unit_price` is captured at checkout time, so historical order lines remain accurate even if a product is later edited or deleted. `CartItem.price_snapshot` and `CartCoupon.discount_amount` serve the same purpose at cart level. `Order → Address` and `Order → PaymentMethod` are live FK references to the records selected at checkout — not copies of the address or payment data.
