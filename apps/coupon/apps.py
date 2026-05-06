"""Coupon app — coupons, constraints, and redemption ledger.

Implements PROJECT_SPEC §2 (coupon operations) and the bonus constraint set
(cart minimum, location, segment, usage caps, validity window, product
allow/deny lists). Redemption uses Redis locks + DB row locks per §3.4.
"""

from django.apps import AppConfig


class CouponConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.coupon"
    label = "coupon"
    verbose_name = "Coupons"
