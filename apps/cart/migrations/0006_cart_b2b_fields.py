# Adds lightweight B2B buyer-metadata fields to Cart.
#
# All three fields are optional (blank=True, default="") so existing carts
# and B2C checkout flows are unaffected.  They are set via the new
# POST /api/v1/cart/set-business-details/ action and snapshotted onto Order
# at checkout time (see apps/order/migrations/0003_order_b2b_fields.py).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cart", "0005_cart_one_active_per_tenant_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="cart",
            name="company_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="cart",
            name="tax_number",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.AddField(
            model_name="cart",
            name="purchase_order_reference",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
    ]
