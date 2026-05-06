"""Migration: expand Order to production schema and add OrderItem.

Implements PROJECT_SPEC §2 op 7 (checkout) and §5.3 (immutable order record).
Replaces the placeholder Order skeleton from 0001_initial with the full
production model: cart/address/payment_method FKs, money snapshot, status FSM,
idempotency_key, version, and the new OrderItem model.
"""

import uuid
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("order", "0001_initial"),
        ("cart", "0002_cart_full_model"),
        ("addresses", "0001_initial"),
        ("payment", "0001_initial"),
        ("tenant", "0001_initial"),
    ]

    operations = [
        # ------------------------------------------------------------------
        # 1. Drop the placeholder Order table from 0001_initial and recreate
        #    it with the full production schema.
        # ------------------------------------------------------------------
        migrations.DeleteModel(name="Order"),
        migrations.CreateModel(
            name="Order",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("user_id", models.UUIDField()),
                (
                    "cart",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="cart.cart",
                    ),
                ),
                (
                    "address",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="addresses.address",
                    ),
                ),
                (
                    "payment_method",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="payment.paymentmethod",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        editable=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="tenant.tenant",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING_PAYMENT", "Pending payment"),
                            ("PAID", "Paid"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="PENDING_PAYMENT",
                        max_length=16,
                    ),
                ),
                (
                    "subtotal",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                (
                    "discount_amount",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                (
                    "total",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                ("currency", models.CharField(max_length=3)),
                ("idempotency_key", models.UUIDField(db_index=True)),
                ("version", models.PositiveIntegerField(default=0)),
            ],
            options={
                "verbose_name": "Order",
                "verbose_name_plural": "Orders",
            },
        ),
        # ------------------------------------------------------------------
        # 2. Add constraints and indexes on Order.
        # ------------------------------------------------------------------
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                check=models.Q(
                    status__in=["PENDING_PAYMENT", "PAID", "FAILED", "CANCELLED"]
                ),
                name="ck_order_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                check=models.Q(subtotal__gte=Decimal("0")),
                name="ck_order_subtotal_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                check=models.Q(discount_amount__gte=Decimal("0")),
                name="ck_order_discount_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                check=models.Q(total__gte=Decimal("0")),
                name="ck_order_total_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                check=models.Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_order_currency_iso4217",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_order_tenant_id",
            ),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["tenant", "user_id", "created_at"],
                name="ix_order_tenant_user_created",
            ),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["tenant", "status"],
                name="ix_order_tenant_status",
            ),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["tenant", "cart"],
                name="ix_order_tenant_cart",
            ),
        ),
        # ------------------------------------------------------------------
        # 3. Create OrderItem.
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name="OrderItem",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="order.order",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        editable=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="tenant.tenant",
                    ),
                ),
                ("product_id", models.UUIDField()),
                ("quantity", models.PositiveIntegerField()),
                (
                    "unit_price",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                ("currency", models.CharField(max_length=3)),
            ],
            options={
                "verbose_name": "Order Item",
                "verbose_name_plural": "Order Items",
            },
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.UniqueConstraint(
                fields=["order", "product_id"],
                name="uq_orderitem_order_product",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.CheckConstraint(
                check=models.Q(quantity__gte=1),
                name="ck_orderitem_qty_pos",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.CheckConstraint(
                check=models.Q(unit_price__gte=Decimal("0")),
                name="ck_orderitem_price_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.CheckConstraint(
                check=models.Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_orderitem_currency_iso4217",
            ),
        ),
        migrations.AddIndex(
            model_name="orderitem",
            index=models.Index(
                fields=["tenant", "order"],
                name="ix_orderitem_tenant_order",
            ),
        ),
    ]
