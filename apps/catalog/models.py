"""Catalog models — ``Product``.

Implements PROJECT_SPEC §3.2 (tenant isolation via TenantAwareModel),
§3.5 (Decimal prices + ISO 4217 currency), §6.2 (UUID primary keys).

``Product`` is the authoritative record of what a tenant offers for sale.
Stock is an integer counter decremented at checkout; no reservation TTL yet
(PROJECT_SPEC §8 — "Real-time inventory" is out of scope for iteration 1).
Cart services hold a ``product_id`` UUID (loose coupling) and look up the
live price/stock here at add-time and again at checkout to detect drift.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from apps.tenant.models import TenantAwareModel


class Product(TenantAwareModel):
    """A product available for purchase within a tenant's catalog.

    Money fields use ``DecimalField`` to avoid floating-point rounding errors
    (PROJECT_SPEC §3.5).  ``currency`` carries the ISO 4217 three-letter code
    that qualifies every price; it is enforced by a DB-level CHECK constraint.

    ``stock`` uses ``PositiveIntegerField`` which maps to a non-negative integer
    column; the field itself prevents negative values at the application layer.
    An additional DB-level CHECK is not strictly needed but is included to
    make the invariant explicit at the schema level.
    """

    # ADR-NOTE §6.2: UUIDv7 (time-ordered) is preferred for B-tree performance
    # at scale. A ``uuid7()`` helper will be introduced in a common ids module
    # in a later iteration; ``uuid.uuid4`` is used in the interim.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=255)

    # Price at time of catalog listing. CartItem captures a ``price_snapshot``
    # at add-time; the original price here may change without affecting in-flight
    # carts until checkout re-validation (PROJECT_SPEC §5.3).
    price = models.DecimalField(max_digits=12, decimal_places=2)

    # ISO 4217 three-letter currency code (e.g. "USD", "SAR", "EUR").
    # Enforced by a CHECK constraint below; CharField max_length=3 matches.
    currency = models.CharField(max_length=3, default="USD")

    # Non-negative available quantity. Decremented at checkout.
    # PositiveIntegerField enforces >= 0 at the ORM layer.
    stock = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Product"
        verbose_name_plural = "Products"
        constraints = [
            # One product name per tenant — avoids duplicate listings.
            models.UniqueConstraint(
                fields=["tenant", "name"],
                name="uq_product_tenant_name",
            ),
            # Prices must be non-negative. The CHECK mirrors the ORM-level
            # PositiveIntegerField guard on stock; having it at the DB level
            # means it holds regardless of ORM bypass (migrations, raw SQL).
            models.CheckConstraint(
                check=Q(price__gte=0),
                name="ck_product_price_nonneg",
            ),
            # Schema-level belt-and-suspenders for stock non-negativity.
            # PositiveIntegerField already prevents negative ORM writes; this
            # CHECK is the last resort against raw SQL or ORM bypass (e.g.
            # a racing stock decrement that slips past the conditional UPDATE
            # guard in CheckoutService).
            models.CheckConstraint(
                check=Q(stock__gte=0),
                name="ck_product_stock_nonneg",
            ),
            # Validate ISO 4217 format (3 uppercase ASCII letters) at the DB.
            # This is a lightweight syntactic check; semantic validity (the code
            # actually exists in the ISO list) is left to the serializer layer.
            models.CheckConstraint(
                check=Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_product_currency_iso4217",
            ),
        ]
        indexes = [
            # Composite index leads with tenant_id per PROJECT_SPEC §3.2 /
            # §6.1. Supports the common query: "list all products for tenant,
            # ordered/filtered by name".
            models.Index(fields=["tenant", "name"], name="ix_product_tenant_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.tenant_id})"
