# Adds lightweight B2B buyer-metadata fields to Order as an immutable snapshot.
#
# These fields are copied from Cart at checkout time.  Empty string when the
# buyer did not supply business details (B2C flow), so no existing orders are
# affected by this migration.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("order", "0002_order_full_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="company_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="order",
            name="tax_number",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.AddField(
            model_name="order",
            name="purchase_order_reference",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
    ]
