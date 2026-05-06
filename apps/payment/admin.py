"""Admin registrations for the payment app."""

from django.contrib import admin

from apps.payment.models import Payment, PaymentMethod


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ("id", "gateway_slug", "tenant", "created_at")
    list_filter = ("tenant", "gateway_slug")
    search_fields = ("id",)
    readonly_fields = ("id", "tenant", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "provider",
        "status",
        "amount",
        "currency",
        "cart",
        "tenant",
        "updated_at",
    )
    list_filter = ("tenant", "status", "provider")
    search_fields = ("id", "provider", "cart__id")
    readonly_fields = ("id", "tenant", "created_at", "updated_at")
    ordering = ("-created_at",)
