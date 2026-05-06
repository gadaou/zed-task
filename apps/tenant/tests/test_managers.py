"""Tests for TenantAwareManager / TenantAwareQuerySet.

Covers:
- all() inside tenant_context(A) returns only tenant A's rows
- all() inside tenant_context(B) returns only tenant B's rows
- all() outside any context raises TenantContextMissing
- all_tenants() returns rows from all tenants
- create() inside tenant_context auto-stamps tenant
- filter() / get() scoped correctly
- get() raises DoesNotExist for a different tenant's object
"""

from __future__ import annotations

import uuid

from django.test import TestCase

from apps.cart.models import Cart
from apps.tenant.context import tenant_context
from apps.tenant.exceptions import TenantContextMissing
from apps.tenant.models import Tenant


def _make_tenant(domain: str, name: str | None = None) -> Tenant:
    return Tenant.objects.create(name=name or domain, domain=domain)


class TenantAwareQuerySetTests(TestCase):
    def setUp(self) -> None:
        self.tenant_a = _make_tenant("a.example.com", "Tenant A")
        self.tenant_b = _make_tenant("b.example.com", "Tenant B")

        self.user_a1 = uuid.uuid4()
        self.user_a2 = uuid.uuid4()
        self.user_b1 = uuid.uuid4()

        with tenant_context(self.tenant_a):
            self.cart_a1 = Cart.objects.create(user_id=self.user_a1)
            self.cart_a2 = Cart.objects.create(user_id=self.user_a2)

        with tenant_context(self.tenant_b):
            self.cart_b1 = Cart.objects.create(user_id=self.user_b1)

    # ------------------------------------------------------------------
    # all()
    # ------------------------------------------------------------------

    def test_all_inside_context_a_returns_only_a_rows(self) -> None:
        with tenant_context(self.tenant_a):
            qs = Cart.objects.all()
        ids = {obj.id for obj in qs}
        self.assertIn(self.cart_a1.id, ids)
        self.assertIn(self.cart_a2.id, ids)
        self.assertNotIn(self.cart_b1.id, ids)

    def test_all_inside_context_b_returns_only_b_rows(self) -> None:
        with tenant_context(self.tenant_b):
            qs = Cart.objects.all()
        ids = {obj.id for obj in qs}
        self.assertIn(self.cart_b1.id, ids)
        self.assertNotIn(self.cart_a1.id, ids)

    def test_all_outside_context_raises_tenant_context_missing(self) -> None:
        with self.assertRaises(TenantContextMissing):
            list(Cart.objects.all())

    # ------------------------------------------------------------------
    # all_tenants()
    # ------------------------------------------------------------------

    def test_all_tenants_returns_rows_from_both_tenants(self) -> None:
        qs = Cart.objects.all_tenants()
        ids = {obj.id for obj in qs}
        self.assertIn(self.cart_a1.id, ids)
        self.assertIn(self.cart_b1.id, ids)

    def test_all_tenants_does_not_require_context(self) -> None:
        # Should not raise even outside a tenant_context.
        count = Cart.objects.all_tenants().count()
        self.assertEqual(count, 3)

    # ------------------------------------------------------------------
    # create() — auto-stamps tenant
    # ------------------------------------------------------------------

    def test_create_stamps_tenant_from_context(self) -> None:
        with tenant_context(self.tenant_a):
            cart = Cart.objects.create(user_id=uuid.uuid4())
        self.assertEqual(cart.tenant_id, self.tenant_a.id)

    def test_create_outside_context_raises(self) -> None:
        with self.assertRaises(TenantContextMissing):
            Cart.objects.create(user_id=uuid.uuid4())

    # ------------------------------------------------------------------
    # filter()
    # ------------------------------------------------------------------

    def test_filter_scoped_to_current_tenant(self) -> None:
        with tenant_context(self.tenant_a):
            qs = Cart.objects.filter(id=self.cart_a1.id)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().id, self.cart_a1.id)

    def test_filter_cannot_see_other_tenant_rows(self) -> None:
        with tenant_context(self.tenant_a):
            qs = Cart.objects.filter(id=self.cart_b1.id)
        self.assertEqual(qs.count(), 0)

    # ------------------------------------------------------------------
    # get()
    # ------------------------------------------------------------------

    def test_get_returns_own_tenant_row(self) -> None:
        with tenant_context(self.tenant_a):
            cart = Cart.objects.get(id=self.cart_a1.id)
        self.assertEqual(cart.id, self.cart_a1.id)

    def test_get_raises_does_not_exist_for_other_tenant_row(self) -> None:
        with tenant_context(self.tenant_a):
            with self.assertRaises(Cart.DoesNotExist):
                Cart.objects.get(id=self.cart_b1.id)

    # ------------------------------------------------------------------
    # Context nesting / reset
    # ------------------------------------------------------------------

    def test_context_nesting_restores_outer_tenant(self) -> None:
        with tenant_context(self.tenant_a):
            with tenant_context(self.tenant_b):
                qs_inner = Cart.objects.all()
                ids_inner = {obj.id for obj in qs_inner}
            # back in tenant_a context
            qs_outer = Cart.objects.all()
            ids_outer = {obj.id for obj in qs_outer}

        self.assertIn(self.cart_b1.id, ids_inner)
        self.assertNotIn(self.cart_b1.id, ids_outer)
        self.assertIn(self.cart_a1.id, ids_outer)
