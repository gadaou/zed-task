# B2B Buyer Flow

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant CartView as CartService / View
    participant CartDB as Cart (PostgreSQL)
    participant CheckoutSvc as CheckoutService
    participant OrderDB as Order (PostgreSQL)
    participant InvSvc as InvoiceService

    note over Client,CartDB: Step 1 — Set business details (optional, B2B buyers only)
    Client->>CartView: POST /api/v1/cart/set-business-details/<br/>{ company_name, tax_number, purchase_order_reference }
    CartView->>CartDB: UPDATE Cart SET<br/>company_name=...<br/>tax_number=...<br/>purchase_order_reference=...<br/>WHERE tenant_id=ctx AND user_id=X-User-Id AND status=ACTIVE
    CartDB-->>CartView: Cart updated
    CartView-->>Client: 200 OK { cart }

    note over Client,CartDB: Step 2 — Normal cart operations (same for B2B and B2C)
    Client->>CartView: POST /api/v1/cart/items/ (add products)
    Client->>CartView: POST /api/v1/cart/coupons/ (optional)
    Client->>CartView: POST /api/v1/cart/set-address/
    Client->>CartView: POST /api/v1/cart/set-payment-method/

    note over Client,OrderDB: Step 3 — Checkout snapshots B2B fields onto Order
    Client->>CheckoutSvc: POST /api/v1/cart/checkout/\nIdempotency-Key
    CheckoutSvc->>CartDB: SELECT Cart FOR UPDATE\n(reads company_name, tax_number,\npurchase_order_reference)
    CheckoutSvc->>OrderDB: INSERT Order (<br/>  ...money snapshot...<br/>  company_name = cart.company_name,<br/>  tax_number = cart.tax_number,<br/>  purchase_order_reference = cart.purchase_order_reference<br/>)
    OrderDB-->>CheckoutSvc: Order row persisted
    CheckoutSvc-->>Client: 202 Accepted

    note over OrderDB,InvSvc: Step 4 — Invoice reads from Order snapshot (not Cart)
    InvSvc->>OrderDB: SELECT Order WHERE id=order_id
    OrderDB-->>InvSvc: Order with B2B fields
    InvSvc->>InvSvc: render invoice PDF using<br/>order.company_name\norder.tax_number\norder.purchase_order_reference
    note over InvSvc: Cart may already be CHECKED_OUT or\ndeleted — invoice never re-queries Cart
```

- B2B metadata (`company_name`, `tax_number`, `purchase_order_reference`) is **optional** on the cart; B2C flows simply leave all three fields as empty strings — no branching in the checkout logic.
- `CheckoutService` **snapshots** the three B2B fields verbatim onto the `Order` row at creation time. Once the order exists, the snapshot is immutable — subsequent edits to the cart (or cart deletion) cannot alter the order record.
- `InvoiceService` reads B2B details exclusively from the `Order` snapshot, never from the `Cart`. This keeps the invoice generation path independent of cart lifecycle and ensures invoices remain consistent even if the cart is later modified or cleaned up.
- The same `Order` row serves both B2C (empty B2B fields) and B2B (populated fields) buyers; the invoice rendering layer conditionally includes the business block only when `company_name` is non-empty.
