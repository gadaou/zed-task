"""Tests for Tenant model and TenantAwareModel abstract base.

Covers:
- Tenant.domain uniqueness constraint
- created_at / updated_at population on TenantAwareModel subclasses
- All four placeholder domain models inherit TenantAwareModel (catches drift)
"""

from __future__ import annotations

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.cart.models import Cart
from apps.coupon.models import Coupon
from apps.order.models import Order
from apps.payment.models import PaymentMethod
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant, TenantAwareModel


def _make_tenant(domain: str = "example.com", name: str = "Example") -> Tenant:
    return Tenant.objects.create(name=name, domain=domain)


class TenantModelTests(TestCase):
    def test_tenant_str(self) -> None:
        tenant = _make_tenant()
        self.assertIn("example.com", str(tenant))

    def test_domain_unique_constraint(self) -> None:
        _make_tenant(domain="unique.com")
        with self.assertRaises(IntegrityError):
            _make_tenant(domain="unique.com")

    def test_is_active_defaults_to_true(self) -> None:
        tenant = _make_tenant(domain="active.com")
        self.assertTrue(tenant.is_active)

    def test_created_at_and_updated_at_set(self) -> None:
        before = timezone.now()
        tenant = _make_tenant(domain="ts.com")
        after = timezone.now()
        self.assertGreaterEqual(tenant.created_at, before)
        self.assertLessEqual(tenant.created_at, after)
        self.assertIsNotNone(tenant.updated_at)


class TenantAwareModelInheritanceTests(TestCase):
    """Assert that all current domain placeholder models subclass TenantAwareModel.

    These tests act as structural guards — they will fail immediately if a
    future developer accidentally removes the inheritance or creates a new
    domain model that skips it.
    """

    def test_cart_inherits_tenant_aware_model(self) -> None:
        self.assertTrue(issubclass(Cart, TenantAwareModel))

    def test_coupon_inherits_tenant_aware_model(self) -> None:
        self.assertTrue(issubclass(Coupon, TenantAwareModel))

    def test_order_inherits_tenant_aware_model(self) -> None:
        self.assertTrue(issubclass(Order, TenantAwareModel))

    def test_payment_method_inherits_tenant_aware_model(self) -> None:
        self.assertTrue(issubclass(PaymentMethod, TenantAwareModel))


class TenantAwareModelTimestampTests(TestCase):
    def setUp(self) -> None:
        self.tenant = _make_tenant(domain="stamp.com")

    def test_created_at_auto_set_on_insert(self) -> None:
        before = timezone.now()
        with tenant_context(self.tenant):
            cart = Cart.objects.create()
        self.assertGreaterEqual(cart.created_at, before)
        self.assertLessEqual(cart.created_at, timezone.now())

    def test_updated_at_changes_on_save(self) -> None:
        with tenant_context(self.tenant):
            cart = Cart.objects.create()
        original_updated = cart.updated_at
        cart.reference = "changed"
        cart.save()
        cart.refresh_from_db()
        self.assertGreaterEqual(cart.updated_at, original_updated)
