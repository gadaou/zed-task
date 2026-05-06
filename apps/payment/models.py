"""Payment models — payment methods, intents, and the gateway plug-in registry.

PROJECT_SPEC §3.3 defines the ``PaymentGateway`` protocol; §2 operations 5
and 7 drive the PaymentMethod and PaymentIntent shapes.

ADR-NOTE: Only the ``PaymentMethod`` skeleton is defined here.  Full fields
(gateway_slug, token, metadata, the PaymentIntent FSM, etc.) land in the
iteration that implements checkout and payment flow (PROJECT_SPEC §4.6).
The class is present now so that the TenantAwareModel inheritance rule
(§3.2) is structurally enforced from day one.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenant.models import TenantAwareModel


class PaymentMethod(TenantAwareModel):
    """A stored payment method belonging to a single tenant.

    Placeholder — real fields land in a subsequent iteration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # ADR-NOTE: `gateway_slug` is kept as the only field because it will
    # remain in the final model as the discriminator.  Other fields come later.
    gateway_slug = models.CharField(max_length=50, default="mock")

    class Meta:
        verbose_name = "Payment Method"
        verbose_name_plural = "Payment Methods"

    def __str__(self) -> str:
        return f"PaymentMethod {self.id} ({self.gateway_slug})"
