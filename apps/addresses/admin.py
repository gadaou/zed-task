"""Admin registrations for the addresses app."""

from django.contrib import admin

from apps.addresses.models import Address


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user_id",
        "country",
        "city",
        "label",
        "is_default",
        "deleted_at",
        "tenant",
    )
    list_filter = ("tenant", "country", "is_default")
    search_fields = ("id", "user_id", "city")
    readonly_fields = ("id", "tenant", "created_at", "updated_at")
    ordering = ("-created_at",)
