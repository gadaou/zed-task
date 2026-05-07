"""Structured logging utilities for cart_system.

Three components are provided, all pure stdlib (no extra dependencies):

``RequestContextFilter``
    A ``logging.Filter`` that injects ``request_id``, ``tenant_id``, and
    ``user_id`` onto every ``LogRecord`` from the per-request ``ContextVar``s.
    Attach it to the root handler in ``LOGGING`` settings.

``JsonFormatter``
    Emits one JSON object per log line — suitable for Datadog/Loki/CloudWatch
    log ingestion without a parsing step.  Reads the standard ``LogRecord``
    fields plus any extra fields that service code attaches via
    ``logger.info("...", extra={"action": "...", "duration_ms": 42, ...})``.

``VerboseTextFormatter``
    Human-readable single-line format for local development and test output.
    Equivalent to the previous ``verbose`` formatter but enriched with the
    three context fields.

``log_event``
    Thin facade used by service-layer lifecycle logs.  Keeps call-sites short
    while ensuring that the ``action`` key always appears in the record::

        log_event(logger, "checkout.completed",
                  cart_id=str(cart_id), duration_ms=elapsed)

Reserved ``extra`` keys used by the codebase
---------------------------------------------
``action``       – machine-readable event name (e.g. ``checkout.completed``)
``outcome``      – ``success`` | ``failed`` | ``timeout`` | ``skipped``
``duration_ms``  – wall-clock duration of the instrumented code path
``cart_id``      – UUID string of the relevant cart
``order_id``     – UUID string of the relevant order
``payment_id``   – UUID string of the relevant payment
``invoice_id``   – UUID string of the relevant invoice
``tenant_id``    – copied from ContextVar; also injected by the filter
``user_id``      – copied from ContextVar; also injected by the filter
``request_id``   – copied from ContextVar; also injected by the filter
``metric``       – present only on ``metric.incr`` records from ``apps.core.metrics``
``component``    – dependency name used in readiness failure events
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from apps.core.context import get_request_id, get_user_id

# ---------------------------------------------------------------------------
# Extra LogRecord fields that are surfaced in JSON output.
# Add to this tuple if new structured fields are introduced.
# ---------------------------------------------------------------------------
_EXTRA_FIELDS: tuple[str, ...] = (
    "action",
    "outcome",
    "duration_ms",
    "cart_id",
    "order_id",
    "payment_id",
    "invoice_id",
    "component",
    "metric",
    "lock_key",
    "provider",
    "reason",
    "key",
)


class RequestContextFilter(logging.Filter):
    """Inject per-request observability fields onto every ``LogRecord``.

    The three fields are read from ``ContextVar``s so they are correctly
    propagated across sync views, async handlers, and nested service calls
    without any explicit argument passing.

    - ``request_id``  — from ``apps.core.context``; ``"-"`` when unset.
    - ``user_id``     — from ``apps.core.context``; ``"-"`` when unset.
    - ``tenant_id``   — from ``apps.tenant.context``; ``"-"`` when unset.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        record.user_id = get_user_id() or "-"

        try:
            from apps.tenant.context import get_current_tenant

            tenant = get_current_tenant()
            record.tenant_id = str(tenant.id) if tenant is not None else "-"
        except Exception:  # noqa: BLE001
            record.tenant_id = "-"

        return True


class JsonFormatter(logging.Formatter):
    """Emit one compact JSON object per log line.

    Core fields (always present):

    .. code-block:: json

        {
            "ts": "2026-05-07T02:00:00.123456",
            "level": "INFO",
            "logger": "apps.order.services",
            "msg": "checkout.completed",
            "request_id": "abc123",
            "tenant_id": "...",
            "user_id": "..."
        }

    Any extra fields attached by the caller via ``extra={...}`` are merged
    into the top-level object so they are searchable without JSON path queries.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Ensure the filter has run (it may not have if a handler was added
        # outside the configured chain — defensive default).
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id() or "-"
        if not hasattr(record, "user_id"):
            record.user_id = get_user_id() or "-"
        if not hasattr(record, "tenant_id"):
            record.tenant_id = "-"

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%f"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": record.request_id,  # type: ignore[attr-defined]
            "tenant_id": record.tenant_id,    # type: ignore[attr-defined]
            "user_id": record.user_id,        # type: ignore[attr-defined]
        }

        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class VerboseTextFormatter(logging.Formatter):
    """Human-readable single-line formatter for dev/test environments.

    Format::

        [2026-05-07 02:00:00] INFO apps.order.services req=abc123 tenant=... user=... checkout.completed
    """

    _fmt = "[{asctime}] {levelname:<8} {name} req={request_id} tenant={tenant_id} user={user_id} {message}"

    def __init__(self) -> None:
        super().__init__(fmt=self._fmt, style="{")

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id() or "-"
        if not hasattr(record, "user_id"):
            record.user_id = get_user_id() or "-"
        if not hasattr(record, "tenant_id"):
            record.tenant_id = "-"

        base = super().format(record)

        # Append any extra fields as key=value pairs for easy grep-ability.
        extras = []
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                extras.append(f"{field}={value}")

        return f"{base} {' '.join(extras)}" if extras else base


# ---------------------------------------------------------------------------
# Lifecycle log helper
# ---------------------------------------------------------------------------


def log_event(
    logger: logging.Logger,
    action: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a structured lifecycle event log record.

    Ensures the ``action`` key is always present in the record so downstream
    log queries can filter on ``action`` without parsing the message text.

    Args:
        logger:  The ``logging.Logger`` for the calling module.
        action:  Machine-readable event name, e.g. ``checkout.completed``.
        level:   Log level (default ``logging.INFO``).
        **fields: Additional structured fields (``cart_id``, ``duration_ms``…)
    """
    extra: dict[str, Any] = {"action": action}
    extra.update(fields)
    logger.log(level, action, extra=extra)
