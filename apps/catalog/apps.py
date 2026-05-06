"""Catalog app — products and inventory.

Implements PROJECT_SPEC Appendix A (apps/catalog/) and §3.5 (Decimal prices
with explicit ISO 4217 currency). Product is the authoritative record of what
a tenant sells; stock is checked at cart-add and re-checked at checkout per
§8 (no reservation system yet).
"""

from django.apps import AppConfig


class CatalogConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.catalog"
    label = "catalog"
    verbose_name = "Catalog"
