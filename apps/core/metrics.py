"""Lightweight in-process metric hooks.

Emits metric events as structured log lines so they are captured by the
existing log pipeline without any additional infrastructure dependency.

In production, replace or wrap ``incr()`` to forward events to your chosen
metrics backend (Prometheus, Datadog StatsD, OpenTelemetry Metrics) without
touching any call-site code.

Usage::

    from apps.core.metrics import incr

    incr("checkout.failed", reason="cart_empty", tenant_id=str(tenant_id))
    incr("payment.authorized", provider="stripe")
    incr("redis.lock_contention", lock_key="lock:checkout:...")

The resulting log record will have:

.. code-block:: json

    {
        "level": "INFO",
        "logger": "apps.core.metrics",
        "msg": "metric.incr",
        "metric": "checkout.failed",
        "reason": "cart_empty",
        "tenant_id": "...",
        "request_id": "...",
        ...
    }

Integrating a real metrics client later
----------------------------------------
Option A — Prometheus (no log coupling):
    Replace the body of ``incr`` with a ``Counter.labels(...).inc()`` call
    after creating a registry of counters keyed by ``name``.

Option B — Datadog DogStatsD:
    ``statsd.increment(name, tags=[f"k:{v}" for k, v in labels.items()])``

Option C — OpenTelemetry Metrics:
    Obtain a ``Meter`` from the global provider and call
    ``meter.create_counter(name).add(1, attributes=labels)``.

In all three options the ``incr()`` signature stays the same.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

# Metric names emitted by this codebase (documented here for discoverability)
# ─────────────────────────────────────────────────────────────────────────────
# checkout.failed               – checkout aborted with a domain or lock error
# checkout.lock_contention      – Redis lock was already held at checkout entry
# payment.authorized            – gateway returned success
# payment.declined              – gateway returned a hard decline
# payment.timeout               – gateway timed out or was unavailable
# idempotency.replay            – identical key+hash seen again; stored response returned
# idempotency.conflict          – same key, different request payload
# idempotency.in_progress       – concurrent request holds the idempotency sentinel
# readiness.dependency_failed   – a readiness check (postgres/redis) failed
# cart.mutation                 – generic cart mutation event (add/remove/address/pm)
# invoice.failed                – invoice generation failed after retries
# ─────────────────────────────────────────────────────────────────────────────


def incr(name: str, **labels: Any) -> None:
    """Record a counter increment as a structured log line.

    Args:
        name:     Metric name in ``domain.event`` dot notation.
        **labels: Key-value tags (e.g. ``provider="stripe"``, ``reason="empty"``).
                  These become top-level JSON fields in the log record so they
                  are filterable without JSON path syntax.
    """
    extra: dict[str, Any] = {"metric": name}
    extra.update(labels)
    _logger.info("metric.incr", extra=extra)
