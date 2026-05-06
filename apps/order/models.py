"""Order models — ``Order``, ``OrderItem``, ``Address``, ``Invoice``.

PROJECT_SPEC §2 checkout operation (7) produces an ``Order``; §4.6 async
processing creates the ``Invoice``.

ADR-NOTE: Only the ``Order`` skeleton is defined here.  Full fields
(order items, addresses, payment intent reference, B2B metadata, invoice
linkage, etc.) land in the iteration that implements checkout
(PROJECT_SPEC §4.3 / §4.6).  The class is present now so that the
TenantAwareModel inheritance rule (§3.2) is structurally enforced from
day one.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenant.models import TenantAwareModel


class Order(TenantAwareModel):
    """Immutable record of a successful checkout, belonging to a single tenant.

    Placeholder — real fields land in a subsequent iteration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # ADR-NOTE: `status` is kept as the only field because it will remain in
    # the final model as the order state indicator.  Other fields come later.
    status = models.CharField(max_length=50, default="pending")

    class Meta:
        verbose_name = "Order"
        verbose_name_plural = "Orders"

    def __str__(self) -> str:
        return f"Order {self.id} ({self.status})"
