# Payment Gateways — Design & Extension Guide

> This document covers the pluggable payment gateway system implemented in
> `apps/payment/`. For the full system specification see
> [PROJECT_SPEC.md](../PROJECT_SPEC.md) §3.3 (pluggable interface) and §8
> (real integrations are out of scope for the current iteration).

---

## 1. Why pluggable?

PROJECT_SPEC §3.3 states:

> "A `PaymentGateway` interface is the only thing the domain knows about.
>  Concrete gateways live behind a registry keyed by gateway slug per tenant.
>  Domain code never imports a gateway module directly."
>
> "Adding a new gateway is: implement the protocol, register it, write a
>  contract test against the shared `PaymentGatewayContractTests`. No core
>  changes."

The design goals are straightforward:

1. **Open/closed principle.** New gateways extend the system without touching
   the service layer, the checkout flow, or the Celery tasks.  The gateway
   slug stored in `PaymentMethod.gateway_slug` (a plain string column) is the
   only coupling between the domain and the gateway layer.

2. **Testability.** `PaymentService` is tested against deterministic dummy
   gateways — no network calls, no API keys, no sandboxes required.  Contract
   tests verify every implementation against the same expectations.

3. **Operational independence.** Each gateway can fail independently.
   Stripe can be broken without affecting HyperPay.  Circuit-breaker logic
   (PROJECT_SPEC §5.1) is per-gateway.

---

## 2. The interface

```python
# apps/payment/gateways/base.py

class PaymentGateway(abc.ABC):
    slug: str  # unique lowercase slug, e.g. "stripe"

    @abc.abstractmethod
    def authorize_payment(
        self,
        order: Any,
        payment_method: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorizationResult: ...

    @abc.abstractmethod
    def capture_payment(self, payment_reference: str) -> CaptureResult: ...

    @abc.abstractmethod
    def void_payment(self, payment_reference: str) -> VoidResult: ...

    @abc.abstractmethod
    def refund_payment(
        self,
        payment_reference: str,
        amount: Decimal | None = None,
    ) -> RefundResult: ...
```

### Method semantics

| Method | Source state | Target state | Result type |
|---|---|---|---|
| `authorize_payment` | `REQUIRES_CONFIRMATION` | `AUTHORIZED` (success) or `FAILED` (decline) | `AuthorizationResult` |
| `capture_payment` | `AUTHORIZED` | `CAPTURED` | `CaptureResult` |
| `void_payment` | `AUTHORIZED` | `CANCELLED` | `VoidResult` |
| `refund_payment` | `CAPTURED` or `SUCCEEDED` | `REFUNDED` | `RefundResult` |

### Result dataclasses

All result types are immutable frozen dataclasses:

```python
@dataclass(frozen=True)
class AuthorizationResult:
    success: bool
    reference: str = ""          # gateway-side auth ID, passed to capture/void
    error_code: str = ""         # machine-readable decline reason
    error_message: str = ""      # human-readable decline reason
    raw: dict[str, Any] = ...    # full gateway response (optional)
```

`CaptureResult`, `VoidResult`, and `RefundResult` follow the same shape.
`RefundResult` additionally carries `refunded_amount: Decimal`.

### Transient vs. terminal errors

Gateways signal errors in two ways:

| Signal | Meaning | `PaymentService` behaviour | Celery behaviour |
|---|---|---|---|
| `result.success = False` | Permanent decline | Transition to `FAILED`, raise `GatewayDeclined` | No retry |
| Raise `GatewayTimeout` | Transient timeout | Leave in current state, let exception propagate | Retry with back-off |
| Raise `GatewayUnavailable` | Transient service error | Same as above | Retry with back-off |

**Gateways must not mutate `Payment` rows.** All ORM writes are the exclusive
responsibility of `PaymentService`.

---

## 3. The registry

### Lifecycle

```
App startup (PaymentConfig.ready())
    └─ import apps.payment.gateways.dummy   # triggers module-level calls
           ├─ register_payment_gateway("dummy_success", DummySuccessGateway)
           ├─ register_payment_gateway("dummy_failing", DummyFailingGateway)
           ├─ register_payment_gateway("dummy_timeout", DummyTimeoutGateway)
           └─ register_payment_gateway("mock", DummySuccessGateway)   # alias

Request time (Celery task runs)
    └─ PaymentService.authorize_payment(payment_id)
           └─ get_payment_gateway(payment.provider)
                  ├─ slug found   → return cached instance
                  └─ slug missing → raise UnsupportedGateway
```

The registry is a **process-wide singleton dict** (`apps.payment.gateways.registry._REGISTRY`).
It is written only at startup (import time) and read during request processing.
No locking is needed for reads; the write window is single-threaded module import.

### API

```python
from apps.payment.gateways import (
    register_payment_gateway,
    get_payment_gateway,
    unregister_payment_gateway,   # test-only
    registered_gateways,          # → list[str]
)

# Register once (e.g. in ready() or at module level in the gateway file):
register_payment_gateway("stripe", StripeGateway)

# Look up at call time:
gw = get_payment_gateway("stripe")    # → StripeGateway instance
gw = get_payment_gateway("nope")      # → raises UnsupportedGateway
```

`register_payment_gateway` raises `ValueError` on a duplicate slug — fail loud
at startup so misconfiguration is immediately visible.

---

## 4. Adding a new gateway — three steps

### Step 1 — Subclass `PaymentGateway`

```python
# apps/payment/gateways/stripe.py

from decimal import Decimal
from typing import Any, Mapping

from apps.payment.gateways.base import (
    AuthorizationResult, CaptureResult, PaymentGateway, RefundResult, VoidResult,
)
from apps.payment.gateways.registry import register_payment_gateway

class StripeGateway(PaymentGateway):
    slug = "stripe"

    def authorize_payment(
        self,
        order: Any,
        payment_method: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorizationResult:
        # Call Stripe SDK; translate result to AuthorizationResult.
        ...

    def capture_payment(self, payment_reference: str) -> CaptureResult: ...
    def void_payment(self, payment_reference: str) -> VoidResult: ...
    def refund_payment(self, payment_reference: str, amount: Decimal | None = None) -> RefundResult: ...

# Auto-register when module is imported:
register_payment_gateway(StripeGateway.slug, StripeGateway)
```

### Step 2 — Import in `PaymentConfig.ready()`

```python
# apps/payment/apps.py

def ready(self) -> None:
    import apps.payment.gateways.dummy   # existing dummies
    import apps.payment.gateways.stripe  # new gateway
```

### Step 3 — Write a contract test

```python
# apps/payment/tests/test_gateways.py

from apps.payment.tests.test_gateways import PaymentGatewayContractTests
from apps.payment.gateways.stripe import StripeGateway

class TestStripeGatewayContract(PaymentGatewayContractTests):
    gateway = StripeGateway()
    expects_success = True   # or False for a sandbox in decline mode
```

That is all. No changes to `PaymentService`, `CheckoutService`, or any
Celery task.

---

## 5. Worked example — `StripeGateway` skeleton

The following is a skeleton that shows the registration pattern and method
signatures.  It does **not** include real Stripe SDK calls.

```python
# apps/payment/gateways/stripe.py

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Mapping

from apps.payment.exceptions import GatewayTimeout, GatewayUnavailable
from apps.payment.gateways.base import (
    AuthorizationResult,
    CaptureResult,
    PaymentGateway,
    RefundResult,
    VoidResult,
)
from apps.payment.gateways.registry import register_payment_gateway

logger = logging.getLogger(__name__)


class StripeGateway(PaymentGateway):
    """Stripe payment gateway integration.

    Requires ``STRIPE_SECRET_KEY`` in settings and the ``stripe`` PyPI package.
    """

    slug = "stripe"

    def authorize_payment(
        self,
        order: Any,
        payment_method: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuthorizationResult:
        try:
            import stripe
            from django.conf import settings

            stripe.api_key = settings.STRIPE_SECRET_KEY
            intent = stripe.PaymentIntent.create(
                amount=int(order.total * 100),   # Stripe uses integer cents
                currency=order.currency.lower(),
                payment_method=payment_method.gateway_token,
                confirm=True,
                metadata=metadata or {},
            )
            return AuthorizationResult(
                success=True,
                reference=intent["id"],
                raw=intent,
            )
        except stripe.error.CardError as exc:
            return AuthorizationResult(
                success=False,
                error_code=exc.code or "card_error",
                error_message=exc.user_message or str(exc),
            )
        except stripe.error.Timeout:
            raise GatewayTimeout("Stripe authorization timed out")
        except stripe.error.APIConnectionError:
            raise GatewayUnavailable("Stripe API unreachable")

    def capture_payment(self, payment_reference: str) -> CaptureResult:
        # Stripe PaymentIntents capture automatically on confirm=True;
        # explicit capture is used when capture_method='manual'.
        ...

    def void_payment(self, payment_reference: str) -> VoidResult:
        ...

    def refund_payment(self, payment_reference: str, amount: Decimal | None = None) -> RefundResult:
        ...


register_payment_gateway(StripeGateway.slug, StripeGateway)
```

---

## 6. Why this design?

### Registry over `if/else`

The alternative — a large `if payment.provider == "stripe": ... elif ...` block in
`PaymentService` — requires a core code change for every new gateway, violates
open/closed, and cannot be tested in isolation.

The registry makes each gateway an independent unit with a stable interface.
A typo in a slug (`"stipe"`) raises `UnsupportedGateway` immediately and
visibly; it does not silently fall through to a wrong branch.

### `PaymentService` owns all ORM writes

Gateways are stateless and know nothing about Django models.  This means:

- Contract tests run without a database.
- Gateways are trivially replaceable with fakes in unit tests.
- FSM transition guards live in one place (`PaymentService`) and apply
  uniformly to every gateway — no chance of a gateway implementation
  accidentally skipping the idempotency check.

### Idempotency at the service layer

`PaymentService` guards each transition with a status-filtered UPDATE:

```python
rows = Payment.objects.filter(
    pk=payment.pk,
    status=Payment.Status.REQUIRES_CONFIRMATION,
).update(status=Payment.Status.AUTHORIZED, ...)
```

If a Celery task is re-delivered after a successful commit, the `WHERE`
clause matches zero rows and the operation is a no-op.  No `SELECT` + compare
+ `UPDATE` race; the atomic UPDATE is the guard.

### String slug, not FK

`Payment.provider` is a `CharField`, not a FK to a `Gateway` model.  This
means:

- Adding a new gateway requires no schema migration.
- Removing a gateway from the registry does not orphan existing `Payment` rows
  (old rows remain readable; they will surface `UnsupportedGateway` if
  `PaymentService` is called on them, which is the correct behaviour — not
  silent data loss).

---

## 7. Testing — using the contract mixin

Every `PaymentGateway` implementation must pass the shared contract.  Import
and subclass `PaymentGatewayContractTests` from
`apps/payment/tests/test_gateways.py`:

```python
from apps.payment.tests.test_gateways import PaymentGatewayContractTests
from apps.payment.gateways.stripe import StripeGateway
import responses   # or pytest-httpx, vcrpy, etc.

class TestStripeGatewayContract(PaymentGatewayContractTests):
    @responses.activate
    def setup_method(self, _method):
        # Mock Stripe HTTP calls here.
        responses.add(responses.POST, "https://api.stripe.com/...", json={...})
        self.gateway = StripeGateway()

    expects_success = True
```

The mixin verifies:

- `gateway.slug` is non-empty.
- `authorize_payment` returns `AuthorizationResult` with the correct
  `success` value.
- Successful authorization returns a non-empty `reference`.
- Declining authorization returns a non-empty `error_code`.
- `capture_payment`, `void_payment`, `refund_payment` return their respective
  result types.
- `authorize_payment` accepts an optional `metadata` dict without error.

PROJECT_SPEC §6.5 requires 100% of `PaymentGateway` implementations to pass
the shared contract.
