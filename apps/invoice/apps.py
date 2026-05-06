"""Invoice app — per-order invoice generation and PDF delivery.

Implements PROJECT_SPEC §2 bonus: Invoice handling. Every successful order
(Order.status == PAID) produces exactly one Invoice, generated asynchronously
by the Celery ``invoices`` queue. Invoice numbers are monotonic and gap-free
per tenant (§6.2 — no sequential public ID leaks; the invoice number is a
display label, not the primary key).
"""

from django.apps import AppConfig


class InvoiceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.invoice"
    label = "invoice"
    verbose_name = "Invoices"
