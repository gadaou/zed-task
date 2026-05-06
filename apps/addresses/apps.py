"""Address app — customer shipping and billing addresses.

Implements PROJECT_SPEC §2 operation 6 (add address) and Appendix A
(``apps/addresses/`` as a first-class domain module).

Addresses are customer-scoped (identified by ``user_id`` UUID) and
tenant-scoped.  They are never hard-deleted: ``deleted_at`` enables GDPR
soft-erasure per §8 while preserving FK integrity on historical orders.
"""

from django.apps import AppConfig


class AddressesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.addresses"
    label = "addresses"
    verbose_name = "Addresses"
