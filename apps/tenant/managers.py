"""Tenant-aware ORM manager and queryset.

``TenantAwareManager`` is the enforcement layer for PROJECT_SPEC §3.2:
"every Manager is tenant-aware: it refuses to return a queryset unless a
tenant is set on the current request context."

Design rules:
- ``get_queryset()`` is the single point where tenant scoping is applied.
  Every derived queryset (filter, get, exclude, …) operates on an already-
  scoped base queryset, avoiding double-filtering and recursion.
- Raises ``TenantContextMissing`` immediately when the context is unset.
- ``all_tenants()`` — explicit, ugly-named escape hatch for admin tooling and
  migrations.  Its use must always be justified in a code comment.
- ``create(**kwargs)`` — auto-stamps ``tenant`` from the context so callers do
  not have to pass it explicitly.

The ``TenantAwareQuerySet`` is used as the base queryset class so that the
``all_tenants()`` escape hatch is also accessible as an instance method on
any derived queryset (e.g. after chaining a filter).
"""

from __future__ import annotations

from django.db import models

from apps.tenant.context import get_current_tenant, is_tenant_set
from apps.tenant.exceptions import TenantContextMissing


class TenantAwareQuerySet(models.QuerySet):
    """Base queryset class for all tenant-aware models.

    The tenant scoping itself is applied in ``TenantAwareManager.get_queryset``
    before this queryset is returned to callers, so individual methods here do
    not need to re-apply the filter (which would cause infinite recursion when
    ``get()`` calls ``filter()`` which clones as a ``TenantAwareQuerySet`` and
    calls ``get()`` again).

    Cross-tenant access is exposed ONLY via ``Model.objects.all_tenants()``
    on the manager — never as a queryset-instance method.  A queryset-level
    ``all_tenants()`` would silently fail to remove already-applied tenant
    filters (``QuerySet.all()`` clones the current queryset including its
    WHERE clauses) and is therefore intentionally absent.
    """


class TenantAwareManager(models.Manager):
    """Manager that automatically scopes every queryset to the current tenant.

    Raises ``TenantContextMissing`` when no tenant is active in the
    current ``ContextVar``.  Use ``all_tenants()`` for cross-tenant access.
    """

    def get_queryset(self) -> TenantAwareQuerySet:
        if not is_tenant_set():
            raise TenantContextMissing()
        return TenantAwareQuerySet(self.model, using=self._db).filter(
            tenant=get_current_tenant()
        )

    def create(self, **kwargs) -> models.Model:  # type: ignore[override]
        """Create an instance, auto-stamping tenant from the current context."""
        if not is_tenant_set():
            raise TenantContextMissing()
        kwargs.setdefault("tenant", get_current_tenant())
        # Bypass get_queryset (which would double-filter) by going straight to
        # the base QuerySet's create.
        return TenantAwareQuerySet(self.model, using=self._db).create(**kwargs)

    def get_or_create(self, defaults=None, **kwargs):  # type: ignore[override]
        if not is_tenant_set():
            raise TenantContextMissing()
        kwargs.setdefault("tenant", get_current_tenant())
        return TenantAwareQuerySet(self.model, using=self._db).get_or_create(
            defaults=defaults, **kwargs
        )

    def update_or_create(self, defaults=None, **kwargs):  # type: ignore[override]
        if not is_tenant_set():
            raise TenantContextMissing()
        kwargs.setdefault("tenant", get_current_tenant())
        return TenantAwareQuerySet(self.model, using=self._db).update_or_create(
            defaults=defaults, **kwargs
        )

    def all_tenants(self) -> TenantAwareQuerySet:
        """Cross-tenant escape hatch — see class docstring for usage rules."""
        return TenantAwareQuerySet(self.model, using=self._db)
