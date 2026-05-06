"""Tenant request context.

A ``ContextVar`` is used (rather than thread-locals) so tenant identity is
correctly isolated per async coroutine as well as per WSGI thread.

PROJECT_SPEC §3.2 / §4.2 — every query must run within an active tenant
context.  The ``tenant_context`` context manager is the canonical way to set
this in tests and Celery tasks.  ``TenantMiddleware`` calls the lower-level
``set_current_tenant`` / ``reset_current_tenant`` pair directly so it can store
the reset token for cleanup inside a ``try/finally`` block.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from contextvars import Token

    from apps.tenant.models import Tenant


# Sentinel for "no tenant set" — distinct from None so callers can tell the
# difference between "never set" and "explicitly cleared".
_UNSET: object = object()

_current_tenant: ContextVar[object] = ContextVar("current_tenant", default=_UNSET)


def get_current_tenant() -> "Tenant | None":
    """Return the active tenant or ``None`` if the context is unset."""
    value = _current_tenant.get()
    if value is _UNSET:
        return None
    return value  # type: ignore[return-value]


def is_tenant_set() -> bool:
    """Return ``True`` when a tenant is active in the current context."""
    return _current_tenant.get() is not _UNSET


def set_current_tenant(tenant: "Tenant") -> "Token":
    """Set *tenant* as the active tenant and return a reset token.

    The caller is responsible for calling ``reset_current_tenant(token)``
    afterwards — prefer the ``tenant_context`` context manager instead.
    """
    return _current_tenant.set(tenant)


def reset_current_tenant(token: "Token") -> None:
    """Reset the context to the state captured in *token*."""
    _current_tenant.reset(token)


@contextlib.contextmanager
def tenant_context(tenant: "Tenant") -> Generator[None, None, None]:
    """Context manager that sets *tenant* as the active tenant.

    Restores the previous context on exit (including on exceptions), so
    nesting is safe.  Use this in tests and Celery task preambles::

        with tenant_context(my_tenant):
            Cart.objects.create(name="acme cart")
    """
    token = set_current_tenant(tenant)
    try:
        yield
    finally:
        reset_current_tenant(token)
