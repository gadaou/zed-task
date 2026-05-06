"""Tenant models.

Implements PROJECT_SPEC §3.2 / §4.2.

``Tenant``          — the root entity every domain model points at.
``TenantAwareModel`` — abstract base that every domain model must inherit.

All domain models that extend ``TenantAwareModel`` automatically:
  - carry a ``tenant`` FK (non-null, protected on delete)
  - carry ``created_at`` / ``updated_at`` timestamps
  - use ``TenantAwareManager`` as their default manager, which enforces tenant
    scoping via a ``ContextVar`` and raises ``TenantContextMissing`` when no
    context is active.

UUID primary keys use ``uuid.uuid4`` for now.
ADR-NOTE: PROJECT_SPEC §6.2 mandates UUIDv7 for B-tree friendliness.  A
dedicated ``cart_system/common/ids.py`` module will provide ``uuid7()`` in a
subsequent iteration; at that point ``default=uuid.uuid4`` is replaced here.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenant.managers import TenantAwareManager


class Tenant(models.Model):
    """A store / customer account that owns all domain data.

    ``domain`` is the primary resolution signal for ``TenantMiddleware`` — it
    must be unique across all tenants.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=253, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tenant"
        verbose_name_plural = "Tenants"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.domain})"


class TenantAwareModel(models.Model):
    """Abstract base model for every domain entity that belongs to a tenant.

    Subclasses automatically get:
    - ``tenant``     — non-null FK to ``Tenant``; deletion of a Tenant is
                       PROTECTED (must deprovision all data first).
    - ``created_at`` — set once at insert.
    - ``updated_at`` — updated on every save.
    - ``objects``    — ``TenantAwareManager`` that auto-filters by tenant and
                       raises ``TenantContextMissing`` when no context is set.

    Usage::

        class Cart(TenantAwareModel):
            id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
            # ...

        with tenant_context(my_tenant):
            Cart.objects.create(...)    # tenant stamped automatically
            Cart.objects.all()          # scoped to my_tenant
    """

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantAwareManager()

    class Meta:
        abstract = True
        # ADR-NOTE: Django does NOT inherit Meta.indexes from abstract base
        # models to concrete subclasses.  Each concrete model must define its
        # own composite indexes.  PROJECT_SPEC §3.2 requires indexes to lead
        # with tenant_id.  The ForeignKey already creates an automatic
        # ``tenant_id`` index; concrete models should add
        # ``Index(fields=["tenant", "...sort_field..."])`` in their own Meta
        # as their fields are added in subsequent iterations.
