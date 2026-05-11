"""Management command: seed_demo_data.

Creates a complete, self-contained demo dataset that lets a reviewer exercise
the checkout API within two minutes.  Every object is created idempotently —
multiple runs are safe and produce exactly one copy of each record.

Usage:
    python manage.py seed_demo_data           # seed everything
    python manage.py seed_demo_data --no-cart # skip cart creation

Stable identifiers (never change between runs):
    Tenant domain : demo.localhost
    Customer UUID : 00000000-0000-0000-0000-000000000001  (DEMO_CUSTOMER_ID)

All IDs for products, coupons, address, payment method, and cart are derived
deterministically via get_or_create so reruns are zero-noise.
"""

from __future__ import annotations

import textwrap
import uuid
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.addresses.models import Address
from apps.cart.models import Cart
from apps.cart.services import add_product_to_cart
from apps.catalog.models import Product
from apps.coupon.models import Coupon
from apps.payment.models import PaymentMethod
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant

DEMO_DOMAIN = "demo.localhost"
DEMO_TENANT_NAME = "Demo Store"
DEMO_CUSTOMER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

DEMO_PRODUCTS = [
    {
        "name": "Wireless Headphones",
        "price": Decimal("79.99"),
        "currency": "USD",
        "stock": 100,
    },
    {
        "name": "Mechanical Keyboard",
        "price": Decimal("129.99"),
        "currency": "USD",
        "stock": 50,
    },
    {
        "name": "USB-C Hub",
        "price": Decimal("34.99"),
        "currency": "USD",
        "stock": 200,
    },
]

DEMO_COUPONS = [
    {
        "code": "DEMO10",
        "discount_type": Coupon.DiscountType.PERCENTAGE,
        "value": Decimal("10"),
        "currency": None,
        "usage_limit": None,
        "description": "10% off everything",
    },
    {
        "code": "SAVE5",
        "discount_type": Coupon.DiscountType.FIXED,
        "value": Decimal("5.00"),
        "currency": "USD",
        "usage_limit": 100,
        "description": "$5 fixed discount",
    },
]


class Command(BaseCommand):
    help = "Seed demo data for reviewer experience. Idempotent — safe to run multiple times."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-cart",
            action="store_true",
            help="Skip demo cart creation.",
        )

    def handle(self, *args, **options):
        skip_cart = options["no_cart"]

        self.stdout.write("\nSeeding demo data...")

        tenant, tenant_created = self._seed_tenant()
        self._print_status("Tenant", tenant.domain, tenant_created)

        with tenant_context(tenant):
            products, product_created_flags = self._seed_products(tenant)
            for p, created in zip(products, product_created_flags):
                self._print_status("Product", p.name, created)

            coupons, coupon_created_flags = self._seed_coupons(tenant)
            for c, created in zip(coupons, coupon_created_flags):
                self._print_status("Coupon", c.code, created)

            address, address_created = self._seed_address(tenant)
            self._print_status("Address", f"id={address.id}", address_created)

            payment_method, pm_created = self._seed_payment_method(tenant)
            self._print_status("PaymentMethod", f"id={payment_method.id}", pm_created)

            cart = None
            if not skip_cart:
                cart, cart_created = self._seed_cart(tenant, products)
                self._print_status("Cart", f"id={cart.id}", cart_created)

        self._print_summary(
            tenant=tenant,
            products=products,
            coupons=coupons,
            address=address,
            payment_method=payment_method,
            cart=cart,
        )

    # ------------------------------------------------------------------
    # Seed helpers — each returns (object, created: bool) or a list variant
    # ------------------------------------------------------------------

    def _seed_tenant(self) -> tuple[Tenant, bool]:
        return Tenant.objects.get_or_create(
            domain=DEMO_DOMAIN,
            defaults={"name": DEMO_TENANT_NAME, "is_active": True},
        )

    def _seed_products(self, tenant: Tenant) -> tuple[list[Product], list[bool]]:
        products = []
        created_flags = []
        for spec in DEMO_PRODUCTS:
            obj, created = Product.objects.all_tenants().get_or_create(
                tenant=tenant,
                name=spec["name"],
                defaults={
                    "price": spec["price"],
                    "currency": spec["currency"],
                    "stock": spec["stock"],
                },
            )
            products.append(obj)
            created_flags.append(created)
        return products, created_flags

    def _seed_coupons(self, tenant: Tenant) -> tuple[list[Coupon], list[bool]]:
        coupons = []
        created_flags = []
        for spec in DEMO_COUPONS:
            obj, created = Coupon.objects.all_tenants().get_or_create(
                tenant=tenant,
                code=spec["code"],
                defaults={
                    "discount_type": spec["discount_type"],
                    "value": spec["value"],
                    "currency": spec["currency"],
                    "usage_limit": spec["usage_limit"],
                    "is_active": True,
                },
            )
            coupons.append(obj)
            created_flags.append(created)
        return coupons, created_flags

    def _seed_address(self, tenant: Tenant) -> tuple[Address, bool]:
        obj, created = Address.objects.all_tenants().get_or_create(
            tenant=tenant,
            user_id=DEMO_CUSTOMER_ID,
            is_default=True,
            deleted_at=None,
            defaults={
                "country": "US",
                "city": "San Francisco",
                "details": "123 Demo Street, Suite 456",
                "label": "Demo HQ",
            },
        )
        return obj, created

    def _seed_payment_method(self, tenant: Tenant) -> tuple[PaymentMethod, bool]:
        existing = (
            PaymentMethod.objects.all_tenants()
            .filter(tenant=tenant, gateway_slug="dummy_success")
            .first()
        )
        if existing:
            return existing, False
        obj = PaymentMethod.objects.all_tenants().create(
            tenant=tenant,
            gateway_slug="dummy_success",
        )
        return obj, True

    @transaction.atomic
    def _seed_cart(self, tenant: Tenant, products: list[Product]) -> tuple[Cart, bool]:
        existing = (
            Cart.objects.all_tenants()
            .filter(tenant=tenant, user_id=DEMO_CUSTOMER_ID, status=Cart.Status.ACTIVE)
            .first()
        )
        if existing:
            return existing, False

        cart = Cart.objects.all_tenants().create(
            tenant=tenant,
            user_id=DEMO_CUSTOMER_ID,
            currency="USD",
        )
        for product in products[:2]:
            add_product_to_cart(cart, product, quantity=1)
            cart.refresh_from_db()

        return cart, True

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def _print_status(self, kind: str, identifier: str, created: bool) -> None:
        action = (
            self.style.SUCCESS("created") if created else self.style.WARNING("exists")
        )
        self.stdout.write(f"  {kind:<16} {identifier:<50} [{action}]")

    def _print_summary(
        self,
        *,
        tenant: Tenant,
        products: list[Product],
        coupons: list[Coupon],
        address: Address,
        payment_method: PaymentMethod,
        cart: Cart | None,
    ) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("  DEMO SEED SUMMARY"))
        self.stdout.write(self.style.SUCCESS("=" * 70))

        self.stdout.write(f"\n  Tenant domain  : {self.style.HTTP_INFO(tenant.domain)}")
        self.stdout.write(f"  Customer UUID  : {DEMO_CUSTOMER_ID}")

        self.stdout.write("\n  Products:")
        for p in products:
            self.stdout.write(f"    {p.id}  {p.name} @ {p.currency} {p.price}")

        self.stdout.write("\n  Coupons:")
        for c in coupons:
            spec = next(s for s in DEMO_COUPONS if s["code"] == c.code)
            self.stdout.write(f"    {c.code:<12}  {spec['description']}")

        self.stdout.write(f"\n  Address ID     : {address.id}")
        self.stdout.write(
            f"  PaymentMethod  : {payment_method.id}  (gateway: {payment_method.gateway_slug})"
        )

        if cart:
            self.stdout.write(
                f"  Cart ID        : {self.style.HTTP_INFO(str(cart.id))}"
            )
            self.stdout.write(f"  Cart total     : USD {cart.total_price}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("  QUICK CHECKOUT (copy & paste)"))
        self.stdout.write(self.style.SUCCESS("=" * 70))

        if cart:
            curl = textwrap.dedent(f"""\
                curl -s -X POST http://localhost:8000/api/v1/carts/{cart.id}/checkout/ \\
                  -H "Content-Type: application/json" \\
                  -H "X-Tenant-Domain: {tenant.domain}" \\
                  -H "Idempotency-Key: $(python3 -c 'import uuid; print(uuid.uuid4())')" \\
                  -d '{{
                    "payment_method_id": "{payment_method.id}",
                    "address_id": "{address.id}"
                  }}' | python3 -m json.tool
            """)
            self.stdout.write("\n" + curl)

            apply_coupon = textwrap.dedent(f"""\
                # Optional: apply the 10% coupon before checkout
                curl -s -X POST http://localhost:8000/api/v1/cart/coupons/ \\
                  -H "Content-Type: application/json" \\
                  -H "X-Tenant-Domain: {tenant.domain}" \\
                  -H "X-User-Id: {DEMO_CUSTOMER_ID}" \\
                  -d '{{"code": "DEMO10"}}' | python3 -m json.tool
            """)
            self.stdout.write(apply_coupon)
        else:
            self.stdout.write(
                f"\n  Re-run without --no-cart to get a ready-to-use cart ID.\n"
                f"  Or create a cart and use address_id={address.id} "
                f"and payment_method_id={payment_method.id}.\n"
            )

        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write("")
