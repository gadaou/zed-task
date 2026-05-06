"""Unit tests for ``payment.services.add_payment_method``.

Covers:
- Registered slug → creates and returns a ``PaymentMethod``.
- Unknown slug → raises ``UnsupportedGateway``.
- The created ``PaymentMethod`` is scoped to the current tenant.
"""

from __future__ import annotations

import uuid

import pytest

from apps.payment.exceptions import UnsupportedGateway
from apps.payment.models import PaymentMethod
from apps.payment.services import add_payment_method
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant


@pytest.fixture
def tenant(db) -> Tenant:
    t = Tenant.objects.create(
        name="PM Test Tenant",
        domain=f"pm-{uuid.uuid4().hex[:8]}.test",
    )
    with tenant_context(t):
        yield t


@pytest.mark.django_db
def test_add_payment_method_creates_record(tenant) -> None:
    pm = add_payment_method(gateway_slug="dummy_success")

    assert pm.pk is not None
    assert pm.gateway_slug == "dummy_success"
    assert PaymentMethod.objects.filter(pk=pm.pk).exists()


@pytest.mark.django_db
def test_add_payment_method_is_tenant_scoped(tenant) -> None:
    pm = add_payment_method(gateway_slug="dummy_success")
    assert pm.tenant_id == tenant.id


@pytest.mark.django_db
def test_add_payment_method_unknown_slug_raises(tenant) -> None:
    with pytest.raises(UnsupportedGateway):
        add_payment_method(gateway_slug="stripe_not_registered_yet")


@pytest.mark.django_db
def test_add_payment_method_multiple_calls_create_separate_rows(tenant) -> None:
    pm1 = add_payment_method(gateway_slug="dummy_success")
    pm2 = add_payment_method(gateway_slug="dummy_success")
    assert pm1.pk != pm2.pk
    assert PaymentMethod.objects.count() == 2
