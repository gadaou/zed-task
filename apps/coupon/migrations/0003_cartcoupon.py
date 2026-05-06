# Hand-authored migration for apps/coupon — introduces the CartCoupon join
# model that records "this coupon is applied to this cart".
# PROJECT_SPEC §2 ops 3–4 (apply/remove coupon — the DELETE endpoint
# .../coupons/{id} is what makes this a many-to-many relation), §3.2 (tenant
# isolation), §6.2 (UUID PKs), §6.6 (additive migrations).
#
# Dependencies:
# - cart 0003_cart_discount_fields lands the discount columns CartCoupon's
#   discount_amount snapshot is summed into; we depend on it so the migration
#   graph guarantees both schemas land before any service code ships.
# - coupon 0002_coupon_full_model is the prior coupon migration in this app.
# - tenant 0001_initial is the source of the tenant FK target.

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cart", "0003_cart_discount_fields"),
        ("coupon", "0002_coupon_full_model"),
        ("tenant", "0001_initial"),
    ]

    operations = [
        # ------------------------------------------------------------------
        # Step 1 — CreateModel CartCoupon.
        # Field order mirrors the model definition: timestamp mixin first
        # (created_at, updated_at from TenantAwareModel), then the PK, then
        # the snapshot data, then the FKs.
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name="CartCoupon",
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
                    "discount_amount",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                (
                    "currency",
                    models.CharField(max_length=3),
                ),
                (
                    "applied_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "cart",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="applied_coupons",
                        to="cart.cart",
                    ),
                ),
                (
                    "coupon",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cart_applications",
                        to="coupon.coupon",
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
                "verbose_name": "Cart Coupon",
                "verbose_name_plural": "Cart Coupons",
            },
        ),
        # ------------------------------------------------------------------
        # Step 2 — Constraints.
        # ------------------------------------------------------------------
        migrations.AddConstraint(
            model_name="cartcoupon",
            constraint=models.UniqueConstraint(
                fields=["cart", "coupon"],
                name="uq_cartcoupon_cart_coupon",
            ),
        ),
        migrations.AddConstraint(
            model_name="cartcoupon",
            constraint=models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_cartcoupon_tenant_id",
            ),
        ),
        migrations.AddConstraint(
            model_name="cartcoupon",
            constraint=models.CheckConstraint(
                check=models.Q(discount_amount__gte=0),
                name="ck_cartcoupon_discount_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="cartcoupon",
            constraint=models.CheckConstraint(
                check=models.Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_cartcoupon_currency_iso4217",
            ),
        ),
        # ------------------------------------------------------------------
        # Step 3 — Indexes.
        # ------------------------------------------------------------------
        migrations.AddIndex(
            model_name="cartcoupon",
            index=models.Index(
                fields=["tenant", "cart"],
                name="ix_cartcoupon_tenant_cart",
            ),
        ),
        migrations.AddIndex(
            model_name="cartcoupon",
            index=models.Index(
                fields=["tenant", "coupon"],
                name="ix_cartcoupon_tenant_coupon",
            ),
        ),
    ]
