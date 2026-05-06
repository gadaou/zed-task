# Hand-authored migration for apps/cart — adds the coupon-related discount
# columns to Cart so a cart read can render the payable amount in one query.
# PROJECT_SPEC §2 ops 3–4 (apply/remove coupon), §5.3 (denormalised totals
# kept in sync by the service layer), §6.6 (additive, backwards compatible).
#
# ADR-NOTE: Both new fields default to Decimal("0.00") so adding the columns
# to a populated table is a pure metadata operation on Postgres 11+ — no row
# rewrite, no lock escalation. The default is preserved (not stripped via
# preserve_default=False) because Cart rows created outside the service layer
# (e.g. via raw SQL or the Django admin) should still satisfy the CHECK
# constraints below; the service layer always overwrites the defaults during
# recalculate_cart().

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cart", "0002_cart_full_model"),
    ]

    operations = [
        # ------------------------------------------------------------------
        # Step 1 — Add the two new denormalised columns on Cart.
        # ------------------------------------------------------------------
        migrations.AddField(
            model_name="cart",
            name="discount_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=14,
            ),
        ),
        migrations.AddField(
            model_name="cart",
            name="total_after_discount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=14,
            ),
        ),
        # ------------------------------------------------------------------
        # Step 2 — CHECK constraints — both fields are non-negative.
        # ck_cart_total_after_discount_nonneg defends against the "fixed
        # coupon larger than subtotal" edge case (the service clamps it,
        # the constraint enforces it at the DB).
        # ------------------------------------------------------------------
        migrations.AddConstraint(
            model_name="cart",
            constraint=models.CheckConstraint(
                check=models.Q(discount_amount__gte=0),
                name="ck_cart_discount_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="cart",
            constraint=models.CheckConstraint(
                check=models.Q(total_after_discount__gte=0),
                name="ck_cart_total_after_discount_nonneg",
            ),
        ),
    ]
