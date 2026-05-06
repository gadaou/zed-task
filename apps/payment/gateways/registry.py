"""Payment gateway registry.

Implements PROJECT_SPEC §3.3: "concrete gateways live behind a registry keyed
by gateway slug per tenant. Domain code never imports a gateway module directly."

The registry is a process-wide dict keyed by gateway slug.  Registration
happens at import time (typically in the gateway's own module via a module-level
call to ``register_payment_gateway``).  Lookup happens at call time inside
``PaymentService``.

Thread safety: ``_REGISTRY`` is only written at process startup (import time)
before any request is handled.  Reads during request processing are lock-free.
If you need to register gateways dynamically at runtime (not recommended),
add your own locking.

Public API (imported via ``apps.payment.gateways``)::

    register_payment_gateway("stripe", StripeGateway)
    gateway = get_payment_gateway("stripe")  # returns a singleton instance
    unregister_payment_gateway("stripe")     # test-only; fails on unknown slug
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.payment.gateways.base import PaymentGateway

logger = logging.getLogger(__name__)

# Module-level registry: slug → singleton gateway instance.
# Populated by ``register_payment_gateway``; never modified after startup.
_REGISTRY: dict[str, "PaymentGateway"] = {}


def register_payment_gateway(
    name: str,
    gateway_class: type["PaymentGateway"],
) -> None:
    """Register a gateway class under a slug.

    ``name`` must be a non-empty lowercase slug (e.g. ``"stripe"``).  The
    class is instantiated once and the instance is cached for the lifetime of
    the process.

    Raises:
        ValueError: if ``name`` is already registered (fail loud to catch
                    accidental double-registration from module re-imports).
        ValueError: if ``name`` is empty.
    """
    if not name:
        raise ValueError("Gateway slug must be a non-empty string.")
    if name in _REGISTRY:
        raise ValueError(
            f"Payment gateway '{name}' is already registered. "
            "If you are overriding it intentionally (e.g. in tests), call "
            "unregister_payment_gateway(name) first."
        )
    _REGISTRY[name] = gateway_class()
    logger.debug("payment.registry: registered gateway '%s' (%s)", name, gateway_class.__name__)


def get_payment_gateway(name: str) -> "PaymentGateway":
    """Return the singleton gateway instance for ``name``.

    Args:
        name: The gateway slug (e.g. ``"stripe"``, ``"dummy_success"``).

    Returns:
        The registered ``PaymentGateway`` instance.

    Raises:
        ``UnsupportedGateway``: if ``name`` has no registration.
    """
    from apps.payment.exceptions import UnsupportedGateway

    gateway = _REGISTRY.get(name)
    if gateway is None:
        raise UnsupportedGateway(slug=name)
    return gateway


def unregister_payment_gateway(name: str) -> None:
    """Remove a gateway from the registry.

    Intended for use in tests only — call this in a fixture teardown to
    restore a clean registry state between test runs.

    Raises:
        KeyError: if ``name`` is not registered (fail loud to catch typos).
    """
    if name not in _REGISTRY:
        raise KeyError(f"Payment gateway '{name}' is not registered; cannot unregister.")
    del _REGISTRY[name]
    logger.debug("payment.registry: unregistered gateway '%s'", name)


def registered_gateways() -> list[str]:
    """Return a sorted list of currently-registered gateway slugs.

    Useful for admin tooling and health-check endpoints.
    """
    return sorted(_REGISTRY)
