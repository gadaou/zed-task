"""Redis-backed rate throttle for cart mutation endpoints.

Uses a fixed-window INCR + EXPIRE counter stored directly in Redis via the
existing ``apps.core.redis.get_redis_client()`` singleton.  This guarantees
correct behaviour across multiple app containers — unlike DRF's default
``django.core.cache``-backed throttle which counts independently per process
when the cache backend is local memory.

Key design
----------
- ``TenantUserScopedThrottle`` — a single reusable ``BaseThrottle`` subclass.
  Each view that uses it sets ``throttle_scope`` (a key into
  ``settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]``) and places
  the class in its ``throttle_classes`` list.
- Counter key format: ``throttle:{scope}:{tenant_id}:{user_id}``.
  Scoping by tenant prevents one tenant's traffic from consuming another's
  allowance; scoping by user_id prevents a single customer from exhausting a
  shared bucket.
- Fixed-window semantics: ``INCR`` increments the counter; ``EXPIRE`` is set
  only on the first increment (``xx=False``), so the window resets naturally
  after ``period`` seconds.
- The ``pipeline()`` call makes INCR + EXPIRE atomic in a single round-trip.

Usage in views::

    from apps.core.throttling import TenantUserScopedThrottle

    class CartCheckoutView(APIView):
        throttle_classes = [TenantUserScopedThrottle]
        throttle_scope = "checkout"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from rest_framework.throttling import BaseThrottle

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView

logger = logging.getLogger(__name__)

_PERIOD_SECONDS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}


def _parse_rate(rate: str) -> tuple[int, int]:
    """Parse a DRF-style rate string (e.g. '10/minute') into (limit, period_seconds)."""
    num, period = rate.split("/", 1)
    period_secs = _PERIOD_SECONDS.get(period.lower())
    if period_secs is None:
        raise ValueError(
            f"Unknown throttle period '{period}'. "
            f"Must be one of: {list(_PERIOD_SECONDS)}"
        )
    return int(num), period_secs


class TenantUserScopedThrottle(BaseThrottle):
    """Redis-backed fixed-window rate limiter scoped to (tenant, user, action).

    Set ``throttle_scope`` on the view class to select the rate from
    ``settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]``.  If the scope is
    not in the rates dict the request is allowed (fail-open — safe for gradual
    rollout).

    Attributes:
        scope: Fallback scope string used when the view does not define
            ``throttle_scope``.  Leave as ``""`` to require views to declare
            their own scope.
    """

    scope: str = ""

    def allow_request(self, request: "Request", view: "APIView") -> bool:  # type: ignore[override]
        scope = getattr(view, "throttle_scope", self.scope)
        rates: dict = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
        rate = rates.get(scope)
        if not rate:
            return True

        limit, period = _parse_rate(rate)

        tenant = getattr(request, "tenant", None)
        tenant_id = str(tenant.id) if tenant else "unknown"
        user_id = request.headers.get("X-User-Id", "anonymous")
        key = f"throttle:{scope}:{tenant_id}:{user_id}"

        from apps.core.redis import get_redis_client

        r = get_redis_client()
        pipe = r.pipeline()
        pipe.incr(key)
        # Set TTL only when the key is brand-new (NX semantics via xx=False).
        # fakeredis 2.x accepts the keyword; real redis-py 5.x does too.
        pipe.expire(key, period, nx=True)
        count, _ = pipe.execute()

        self._count: int = count
        self._limit: int = limit

        if count > limit:
            logger.warning(
                "throttle.exceeded scope=%s tenant=%s user=%s count=%d limit=%d",
                scope,
                tenant_id,
                user_id,
                count,
                limit,
            )
            return False
        return True

    def wait(self) -> float | None:  # type: ignore[override]
        """Return ``None`` — we do not advertise a Retry-After delay."""
        return None
