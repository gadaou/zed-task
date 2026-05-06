"""Address models — ``Address``.

Implements PROJECT_SPEC §2 operation 6 (add address) and Appendix A.

Design decisions:
- ``user_id`` is a bare UUIDField — no FK to ``auth.User`` — matching the
  same decoupling pattern used by ``Cart.user_id`` (PROJECT_SPEC §8: SSO /
  identity provider integration is out of scope).  Address lookups are always
  scoped to ``(tenant_id, user_id)``.
- ``country`` is stored as ISO 3166-1 alpha-2 (2 uppercase letters).  A DB
  CHECK enforces the format; semantic validity (the code actually exists in the
  ISO list) is the serializer's responsibility.
- ``details`` is an intentionally opaque TextField covering street line,
  postal code, apartment number, etc.  ADR-NOTE: we defer structured address
  fields (line1/line2/postal_code/state) to a future iteration so we don't
  lock in a schema before internationalisation requirements are clear.  The
  current shape is sufficient for order fulfilment and coupon country-constraint
  evaluation.
- ``is_default`` tracks the customer's preferred address for a tenant.  A
  partial unique constraint on ``(tenant, user_id)`` WHERE is_default=TRUE and
  deleted_at IS NULL enforces at most one live default per user.  This is a
  Postgres partial index — acceptable per PROJECT_SPEC §4.8 (Postgres is the
  only supported DB).
- ``deleted_at`` is a soft-delete timestamp (PROJECT_SPEC §8 GDPR posture).
  Hard deletes are never performed on addresses; a future orchestrated erasure
  job NULL-ifies PII and sets deleted_at.  Queries in services must filter
  ``deleted_at__isnull=True``; the TenantAwareManager does NOT add this filter
  automatically — explicit filtering is preferred for clarity.

ADR-NOTE §6.2: UUID primary key uses ``uuid.uuid4`` in this iteration.  Replace
with ``uuid7()`` from common/ids.py when that module lands.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q

from apps.tenant.models import TenantAwareModel


class Address(TenantAwareModel):
    """A shipping or billing address belonging to a customer within a tenant.

    Addresses are soft-deleted (``deleted_at`` is set, the row is retained).
    At most one address per ``(tenant, user_id)`` may be the default at any
    given time (enforced by the partial unique constraint below).
    """

    # ADR-NOTE §6.2: uuid4 → uuid7 migration, same as Cart/Product/Coupon.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Loose reference to the customer.  No FK to auth.User (see module
    # docstring).  Always queried together with tenant_id.
    user_id = models.UUIDField()

    # ISO 3166-1 alpha-2 uppercase two-letter country code.
    # DB CHECK ck_address_country_iso3166 validates the format.
    country = models.CharField(max_length=2)

    city = models.CharField(max_length=120)

    # Opaque address details (street line, postal code, apartment, etc.).
    # ADR-NOTE: structured fields (line1/line2/postal_code/state/district) land
    # in a future iteration once internationalisation requirements stabilise.
    details = models.TextField()

    # Free-text label for the customer's own organisation ("home", "office",
    # company name, …).  Optional; blank is permitted.
    label = models.CharField(max_length=50, blank=True, default="")

    # At most one default per (tenant, user) among non-deleted addresses.
    # Enforced by partial unique constraint uq_address_one_default_per_user.
    is_default = models.BooleanField(default=False)

    # Soft-delete timestamp.  NULL = live; non-NULL = logically deleted.
    # Set by the service layer; never hard-deleted.
    # db_index=True adds a simple index on deleted_at to support sweep queries.
    deleted_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    class Meta:
        verbose_name = "Address"
        verbose_name_plural = "Addresses"
        constraints = [
            # Country code must be exactly two uppercase ASCII letters
            # (ISO 3166-1 alpha-2 format check).
            models.CheckConstraint(
                check=Q(country__regex=r"^[A-Z]{2}$"),
                name="ck_address_country_iso3166",
            ),
            # City must not be an empty string.
            models.CheckConstraint(
                check=~Q(city=""),
                name="ck_address_city_nonempty",
            ),
            # Forward-compat composite FK scaffold — mirrors uq_cart_tenant_id
            # and uq_coupon_tenant_id (PROJECT_SPEC §3.2).
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_address_tenant_id",
            ),
            # At most one live default address per (tenant, user).
            # This is a Postgres partial index on a UniqueConstraint —
            # valid since Django 3.0 + Postgres 9.5+ (PROJECT_SPEC §4.8).
            models.UniqueConstraint(
                fields=["tenant", "user_id"],
                condition=Q(is_default=True, deleted_at__isnull=True),
                name="uq_address_one_default_per_user",
            ),
        ]
        indexes = [
            # Primary access pattern: "all addresses for a user in this tenant".
            models.Index(
                fields=["tenant", "user_id"],
                name="ix_address_tenant_user",
            ),
            # Fast path for the most common read: "the default address for a
            # user" — used by checkout to pre-populate shipping.
            models.Index(
                fields=["tenant", "user_id", "is_default"],
                name="ix_address_tenant_user_default",
            ),
            # Admin / analytics: "all addresses in tenant X from country Y".
            models.Index(
                fields=["tenant", "country"],
                name="ix_address_tenant_country",
            ),
        ]

    def __str__(self) -> str:
        return f"Address {self.id} — {self.city}, {self.country} (user {self.user_id})"
