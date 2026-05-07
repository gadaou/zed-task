"""Tests for the seed_demo_data management command.

Covers:
- All required objects are created on first run.
- A second run is a no-op (idempotency).
- --no-cart flag suppresses cart creation.
- Summary output contains expected identifiers.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.addresses.models import Address
from apps.cart.models import Cart, CartItem
from apps.catalog.models import Product
from apps.coupon.models import Coupon
from apps.core.management.commands.seed_demo_data import (
    DEMO_CUSTOMER_ID,
    DEMO_DOMAIN,
    DEMO_PRODUCTS,
    DEMO_COUPONS,
)
from apps.payment.models import PaymentMethod
from apps.tenant.models import Tenant


def _run_seed(**kwargs) -> str:
    """Run the command and return its stdout as a string."""
    out = StringIO()
    call_command("seed_demo_data", stdout=out, **kwargs)
    return out.getvalue()


class SeedDemoDataCreationTests(TestCase):
    """Objects are created correctly on the first run."""

    def setUp(self):
        _run_seed()

    def test_tenant_created(self):
        self.assertTrue(Tenant.objects.filter(domain=DEMO_DOMAIN).exists())

    def test_tenant_is_active(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        self.assertTrue(tenant.is_active)

    def test_all_products_created(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        for spec in DEMO_PRODUCTS:
            self.assertTrue(
                Product.objects.all_tenants()
                .filter(tenant=tenant, name=spec["name"])
                .exists(),
                f"Product '{spec['name']}' was not created",
            )

    def test_product_count(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        count = Product.objects.all_tenants().filter(tenant=tenant).count()
        self.assertEqual(count, len(DEMO_PRODUCTS))

    def test_all_coupons_created(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        for spec in DEMO_COUPONS:
            self.assertTrue(
                Coupon.objects.all_tenants()
                .filter(tenant=tenant, code=spec["code"])
                .exists(),
                f"Coupon '{spec['code']}' was not created",
            )

    def test_coupon_count(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        count = Coupon.objects.all_tenants().filter(tenant=tenant).count()
        self.assertEqual(count, len(DEMO_COUPONS))

    def test_coupons_are_active(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        inactive = (
            Coupon.objects.all_tenants().filter(tenant=tenant, is_active=False).count()
        )
        self.assertEqual(inactive, 0)

    def test_address_created(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        self.assertTrue(
            Address.objects.all_tenants()
            .filter(
                tenant=tenant,
                user_id=DEMO_CUSTOMER_ID,
                is_default=True,
                deleted_at=None,
            )
            .exists()
        )

    def test_payment_method_created(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        self.assertTrue(
            PaymentMethod.objects.all_tenants()
            .filter(tenant=tenant, gateway_slug="dummy_success")
            .exists()
        )

    def test_cart_created_by_default(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        self.assertTrue(
            Cart.objects.all_tenants()
            .filter(tenant=tenant, user_id=DEMO_CUSTOMER_ID, status=Cart.Status.ACTIVE)
            .exists()
        )

    def test_cart_has_items(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        cart = Cart.objects.all_tenants().get(
            tenant=tenant, user_id=DEMO_CUSTOMER_ID, status=Cart.Status.ACTIVE
        )
        item_count = CartItem.objects.all_tenants().filter(cart=cart).count()
        self.assertGreater(item_count, 0)

    def test_cart_total_is_positive(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        cart = Cart.objects.all_tenants().get(
            tenant=tenant, user_id=DEMO_CUSTOMER_ID, status=Cart.Status.ACTIVE
        )
        self.assertGreater(cart.total_price, 0)


class SeedDemoDataIdempotencyTests(TestCase):
    """Running the command twice produces the same single set of objects."""

    def setUp(self):
        _run_seed()
        _run_seed()  # second run

    def _tenant(self) -> Tenant:
        return Tenant.objects.get(domain=DEMO_DOMAIN)

    def test_only_one_tenant(self):
        self.assertEqual(Tenant.objects.filter(domain=DEMO_DOMAIN).count(), 1)

    def test_product_count_unchanged(self):
        count = Product.objects.all_tenants().filter(tenant=self._tenant()).count()
        self.assertEqual(count, len(DEMO_PRODUCTS))

    def test_coupon_count_unchanged(self):
        count = Coupon.objects.all_tenants().filter(tenant=self._tenant()).count()
        self.assertEqual(count, len(DEMO_COUPONS))

    def test_only_one_default_address(self):
        count = (
            Address.objects.all_tenants()
            .filter(
                tenant=self._tenant(),
                user_id=DEMO_CUSTOMER_ID,
                is_default=True,
                deleted_at=None,
            )
            .count()
        )
        self.assertEqual(count, 1)

    def test_only_one_payment_method(self):
        count = (
            PaymentMethod.objects.all_tenants()
            .filter(tenant=self._tenant(), gateway_slug="dummy_success")
            .count()
        )
        self.assertEqual(count, 1)

    def test_only_one_active_cart(self):
        count = (
            Cart.objects.all_tenants()
            .filter(
                tenant=self._tenant(),
                user_id=DEMO_CUSTOMER_ID,
                status=Cart.Status.ACTIVE,
            )
            .count()
        )
        self.assertEqual(count, 1)


class SeedDemoDataNoCartFlagTests(TestCase):
    """--no-cart suppresses cart creation."""

    def test_no_cart_created_with_flag(self):
        _run_seed(no_cart=True)
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        self.assertFalse(
            Cart.objects.all_tenants()
            .filter(tenant=tenant, user_id=DEMO_CUSTOMER_ID)
            .exists()
        )

    def test_other_objects_still_created_with_no_cart(self):
        _run_seed(no_cart=True)
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        self.assertEqual(
            Product.objects.all_tenants().filter(tenant=tenant).count(),
            len(DEMO_PRODUCTS),
        )
        self.assertEqual(
            Coupon.objects.all_tenants().filter(tenant=tenant).count(),
            len(DEMO_COUPONS),
        )


class SeedDemoDataOutputTests(TestCase):
    """stdout contains expected identifiers and curl snippet."""

    def setUp(self):
        self.output = _run_seed()

    def test_output_contains_tenant_domain(self):
        self.assertIn(DEMO_DOMAIN, self.output)

    def test_output_contains_customer_uuid(self):
        self.assertIn(str(DEMO_CUSTOMER_ID), self.output)

    def test_output_contains_coupon_codes(self):
        for spec in DEMO_COUPONS:
            self.assertIn(spec["code"], self.output)

    def test_output_contains_product_names(self):
        for spec in DEMO_PRODUCTS:
            self.assertIn(spec["name"], self.output)

    def test_output_contains_checkout_curl(self):
        self.assertIn("curl", self.output)
        self.assertIn("/checkout/", self.output)
        self.assertIn("X-Tenant-Domain", self.output)
        self.assertIn("Idempotency-Key", self.output)

    def test_output_contains_cart_id(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        cart = Cart.objects.all_tenants().get(
            tenant=tenant, user_id=DEMO_CUSTOMER_ID, status=Cart.Status.ACTIVE
        )
        self.assertIn(str(cart.id), self.output)

    def test_output_contains_payment_method_id(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        pm = PaymentMethod.objects.all_tenants().get(
            tenant=tenant, gateway_slug="dummy_success"
        )
        self.assertIn(str(pm.id), self.output)

    def test_output_contains_address_id(self):
        tenant = Tenant.objects.get(domain=DEMO_DOMAIN)
        addr = Address.objects.all_tenants().get(
            tenant=tenant, user_id=DEMO_CUSTOMER_ID, is_default=True, deleted_at=None
        )
        self.assertIn(str(addr.id), self.output)
