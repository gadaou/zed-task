# Invoice Generation Flow

```mermaid
sequenceDiagram
    autonumber
    participant PaySvc as PaymentService
    participant DB as PostgreSQL
    participant Celery
    participant InvSvc as InvoiceService
    participant PDF as PDFRenderer

    PaySvc->>DB: UPDATE Order status=PAID
    PaySvc->>DB: enqueue_generate_invoice (on_commit hook)
    note over DB: commit

    DB-->>Celery: generate_invoice task dispatched<br/>(queue: invoices)

    Celery->>InvSvc: generate_invoice_for_order(order_id)

    rect rgb(220, 240, 255)
        note over InvSvc,DB: Phase 1 — inside transaction.atomic()
        InvSvc->>DB: SELECT InvoiceSequence FOR UPDATE<br/>(get_or_create per tenant)
        DB-->>InvSvc: sequence row locked
        InvSvc->>DB: UPDATE InvoiceSequence<br/>SET last_number = last_number + 1
        InvSvc->>DB: INSERT Invoice<br/>(number, total, taxes, currency, pdf_url="")
        note over DB: OneToOneField(order) — duplicate INSERT raises IntegrityError
        DB-->>InvSvc: Invoice row created (pdf_url still empty)
    end

    rect rgb(220, 255, 220)
        note over InvSvc,PDF: Phase 2 — outside transaction (file I/O)
        InvSvc->>PDF: render_pdf(invoice, order)
        PDF-->>InvSvc: pdf_bytes
        InvSvc->>InvSvc: write file to MEDIA_ROOT/invoices/{id}.pdf
        InvSvc->>DB: UPDATE Invoice SET pdf_url=path<br/>WHERE id=invoice.id AND pdf_url=""
        note over DB: status-guarded UPDATE — idempotent if pdf_url already set
        DB-->>InvSvc: 1 row updated
    end

    InvSvc-->>Celery: done

    note over Celery: retry path (PDF failure)
    alt PDF render or write fails
        PDF-->>InvSvc: exception
        InvSvc-->>Celery: raise (propagates to Celery)
        Celery->>Celery: retry task
        note over Celery: Phase 1 re-runs but INSERT raises IntegrityError\n(OneToOneField) → caught → Phase 1 is no-op\nPhase 2 re-runs from scratch
    end
```

- **Phase 1** (sequence allocation + `Invoice` row insertion) runs inside `transaction.atomic()`. The `OneToOneField(order)` constraint makes a duplicate insert raise `IntegrityError`, which `InvoiceService` catches and treats as a no-op — safe under at-least-once Celery delivery.
- **Phase 2** (PDF rendering + file write) runs **outside** the transaction. Long-running I/O never holds a database lock; the `UPDATE … WHERE pdf_url=""` guard makes the file write idempotent on retry.
- The invoice number is allocated via `SELECT FOR UPDATE` on `InvoiceSequence`, guaranteeing **gap-free monotonic numbering** per tenant without relying on a PostgreSQL sequence or advisory lock.
- `enqueue_generate_invoice` is called via `transaction.on_commit` inside `PaymentService`, so the invoice task is never queued for an order whose payment transition was rolled back.
