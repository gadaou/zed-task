"""Admin registrations for the invoice app."""

from django.contrib import admin

from apps.invoice.models import Invoice, InvoiceSequence


@admin.register(InvoiceSequence)
class InvoiceSequenceAdmin(admin.ModelAdmin):
    list_display = ("tenant", "last_number")
    search_fields = ("tenant__domain",)
    readonly_fields = ("tenant",)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "tenant", "order", "total", "taxes", "currency", "created_at")
    list_filter = ("currency",)
    search_fields = ("order__id",)
    readonly_fields = ("tenant", "order", "number", "total", "taxes", "currency", "pdf_url", "created_at")
