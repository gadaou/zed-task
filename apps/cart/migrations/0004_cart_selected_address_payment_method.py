# Additive migration — adds two optional FK columns to Cart so the
# POST /cart/add-address and POST /cart/add-payment-method actions can store
# the customer's current selections and pass them forward to checkout.
#
# Both columns are nullable so existing Cart rows remain valid with no backfill
# required (PROJECT_SPEC §6.6 — backwards-compatible, no row rewrite).
# on_delete=PROTECT mirrors the same guard used on Payment→Cart (an in-use
# address/payment-method must not be silently deleted out from under a cart).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("addresses", "0001_initial"),
        ("cart", "0003_cart_discount_fields"),
        ("payment", "0004_alter_paymentmethod_gateway_slug"),
    ]

    operations = [
        migrations.AddField(
            model_name="cart",
            name="selected_address",
            field=models.ForeignKey(
                blank=True,
                default=None,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="addresses.address",
            ),
        ),
        migrations.AddField(
            model_name="cart",
            name="selected_payment_method",
            field=models.ForeignKey(
                blank=True,
                default=None,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="payment.paymentmethod",
            ),
        ),
    ]
