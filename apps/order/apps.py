"""Order app — orders, addresses, and (later) invoices.

Implements PROJECT_SPEC §2 (add address, checkout terminal state) and the
bonus invoice handling. Orders are immutable once created; mutations go
through related entities (refunds, fulfilment events) per §5.3.
"""

from django.apps import AppConfig


class OrderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.order"
    label = "order"
    verbose_name = "Orders"
