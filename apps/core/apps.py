"""Core app — cross-cutting concerns shared by every other app.

Future home of: tenant context plumbing, UUIDv7 generator, problem+json
exception handler, structured logging filters, idempotency primitives,
common abstract base models, health check views.

See PROJECT_SPEC.md §4 (Architectural Principles) and §6 (Engineering
Standards) for the contract this app must satisfy.
"""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = "Core"
