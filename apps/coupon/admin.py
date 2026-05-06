"""Admin registrations for the coupon app."""

from django.contrib import admin

from apps.coupon.models import Coupon


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "discount_type",
        "value",
        "currency",
        "is_active",
        "usage_limit",
        "used_count",
        "starts_at",
        "ends_at",
        "tenant",
    )
    list_filter = ("tenant", "discount_type", "is_active")
    search_fields = ("code", "id")
    readonly_fields = ("id", "tenant", "used_count", "created_at", "updated_at")
    ordering = ("-created_at",)
