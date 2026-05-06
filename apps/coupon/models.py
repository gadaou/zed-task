"""Coupon models.

PROJECT_SPEC §2 operations 3-4 (apply/remove coupon) and the bonus coupon
constraints section describe the full shape.

ADR-NOTE: Only the ``Coupon`` skeleton is defined here.  Full fields
(code, discount_type, constraints, usage caps, validity window, etc.) land
in the iteration that implements coupon application.  The class is present
now so that the TenantAwareModel inheritance rule (PROJECT_SPEC §3.2) is
structurally enforced from day one.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenant.models import TenantAwareModel


class Coupon(TenantAwareModel):
    """Discount coupon belonging to a single tenant.

    Placeholder — real fields land in a subsequent iteration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # ADR-NOTE: `code` is kept as the only field because it is the natural
    # identifier and will remain in the final model.  Other fields come later.
    code = models.CharField(max_length=100)

    class Meta:
        verbose_name = "Coupon"
        verbose_name_plural = "Coupons"
        # (tenant, code) uniqueness enforced at DB level in a subsequent
        # migration once the full model is fleshed out.

    def __str__(self) -> str:
        return f"Coupon {self.code}"
