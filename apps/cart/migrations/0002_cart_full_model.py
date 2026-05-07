# Hand-authored migration for apps/cart — replaces the placeholder Cart model
# with the full Cart + new CartItem models.
# PROJECT_SPEC §3.2, §3.4, §3.5, §6.2
#
# ADR-NOTE: user_id is added with preserve_default=False so the one-off zero
# UUID is applied to any existing stub rows and then the column is made
# non-nullable. No real data loss occurs because the 0001 Cart model held
# only a placeholder ``reference`` field with no business meaning.

import uuid
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cart", "0001_initial"),
        ("tenant", "0001_initial"),
    ]

    operations = [
        # ------------------------------------------------------------------
        # Step 1 — Remove the placeholder field from the stub Cart model.
        # ------------------------------------------------------------------
        migrations.RemoveField(
            model_name="cart",
            name="reference",
        ),
        # ------------------------------------------------------------------
        # Step 2 — Add new Cart fields.
        # user_id: added nullable first so existing rows can be backfilled,
        # then made non-nullable (preserve_default=False drops the default).
        # ------------------------------------------------------------------
        migrations.AddField(
            model_name="cart",
            name="user_id",
            field=models.UUIDField(
                default=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="cart",
            name="status",
            field=models.CharField(
                choices=[("ACTIVE", "Active"), ("CHECKED_OUT", "Checked out")],
                default="ACTIVE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="cart",
            name="total_price",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name="cart",
            name="currency",
            field=models.CharField(default="USD", max_length=3),
        ),
        migrations.AddField(
            model_name="cart",
            name="version",
            field=models.PositiveIntegerField(default=0),
        ),
        # ------------------------------------------------------------------
        # Step 3 — Cart constraints.
        # ------------------------------------------------------------------
        migrations.AddConstraint(
            model_name="cart",
            constraint=models.CheckConstraint(
                check=models.Q(total_price__gte=0),
                name="ck_cart_total_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="cart",
            constraint=models.CheckConstraint(
                check=models.Q(status__in=["ACTIVE", "CHECKED_OUT"]),
                name="ck_cart_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="cart",
            constraint=models.CheckConstraint(
                check=models.Q(currency__regex="^[A-Z]{3}$"),
                name="ck_cart_currency_iso4217",
            ),
        ),
        migrations.AddConstraint(
            model_name="cart",
            constraint=models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_cart_tenant_id",
            ),
        ),
        # ------------------------------------------------------------------
        # Step 4 — Cart indexes.
        # ------------------------------------------------------------------
        migrations.AddIndex(
            model_name="cart",
            index=models.Index(
                fields=["tenant", "user_id"],
                name="ix_cart_tenant_user",
            ),
        ),
        migrations.AddIndex(
            model_name="cart",
            index=models.Index(
                fields=["tenant", "status"],
                name="ix_cart_tenant_status",
            ),
        ),
        migrations.AddIndex(
            model_name="cart",
            index=models.Index(
                fields=["tenant", "user_id", "status"],
                name="ix_cart_tenant_user_status",
            ),
        ),
        # ------------------------------------------------------------------
        # Step 5 — Create CartItem.
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name="CartItem",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True),
                ),
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
                    "product_id",
                    models.UUIDField(),
                ),
                (
                    "quantity",
                    models.PositiveIntegerField(),
                ),
                (
                    "price_snapshot",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "currency",
                    models.CharField(default="USD", max_length=3),
                ),
                (
                    "cart",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="cart.cart",
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
            ],
            options={
                "verbose_name": "Cart Item",
                "verbose_name_plural": "Cart Items",
            },
        ),
        # ------------------------------------------------------------------
        # Step 6 — CartItem constraints.
        # ------------------------------------------------------------------
        migrations.AddConstraint(
            model_name="cartitem",
            constraint=models.UniqueConstraint(
                fields=["cart", "product_id"],
                name="uq_cartitem_cart_product",
            ),
        ),
        migrations.AddConstraint(
            model_name="cartitem",
            constraint=models.CheckConstraint(
                check=models.Q(quantity__gte=1),
                name="ck_cartitem_qty_pos",
            ),
        ),
        migrations.AddConstraint(
            model_name="cartitem",
            constraint=models.CheckConstraint(
                check=models.Q(price_snapshot__gte=0),
                name="ck_cartitem_price_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="cartitem",
            constraint=models.CheckConstraint(
                check=models.Q(currency__regex="^[A-Z]{3}$"),
                name="ck_cartitem_currency_iso4217",
            ),
        ),
        # ------------------------------------------------------------------
        # Step 7 — CartItem indexes.
        # ------------------------------------------------------------------
        migrations.AddIndex(
            model_name="cartitem",
            index=models.Index(
                fields=["tenant", "cart"],
                name="ix_cartitem_tenant_cart",
            ),
        ),
        migrations.AddIndex(
            model_name="cartitem",
            index=models.Index(
                fields=["tenant", "product_id"],
                name="ix_cartitem_tenant_product",
            ),
        ),
    ]
