"""Cart app — the cart aggregate and its line items.

Implements PROJECT_SPEC §2 operations 1–4 (add/remove product, apply/remove
coupon binding) and §4.3 (transactional, version-checked writes).
"""

from django.apps import AppConfig


class CartConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cart"
    label = "cart"
    verbose_name = "Carts"
