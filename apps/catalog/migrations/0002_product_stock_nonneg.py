"""Migration: add ck_product_stock_nonneg CHECK constraint to Product.

PROJECT_SPEC §3.4 / §5.3 — the conditional UPDATE in CheckoutService already
prevents stock from going negative, but a schema-level CHECK is the last-resort
safety net against raw SQL or ORM bypass.

``PositiveIntegerField`` prevents negative values at the ORM layer; this
CHECK makes the invariant explicit at the database layer independently of the
application code.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                check=models.Q(stock__gte=0),
                name="ck_product_stock_nonneg",
            ),
        ),
    ]
