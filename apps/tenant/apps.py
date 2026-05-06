"""Tenant app — owns the tenant model and tenant-resolution middleware.

Tenancy is the foundational concern (PROJECT_SPEC §3.2 / §4.2). This app
exposes:

* ``Tenant`` model — the root entity every other domain model points at via
  ``tenant_id``.
* ``TenantMiddleware`` — resolves the active tenant from the request and
  publishes it through a ``ContextVar`` so service-layer code can never
  accidentally cross tenants.
* ``TenantAwareManager`` — base ORM manager that enforces tenant scoping.
"""

from django.apps import AppConfig


class TenantConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenant"
    label = "tenant"
    verbose_name = "Tenants"
