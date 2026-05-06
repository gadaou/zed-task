# Generated migration for apps/catalog — Product model.
# PROJECT_SPEC §3.2, §3.5, §6.2

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenant", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Product",
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
                    "name",
                    models.CharField(max_length=255),
                ),
                (
                    "price",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                (
                    "currency",
                    models.CharField(default="USD", max_length=3),
                ),
                (
                    "stock",
                    models.PositiveIntegerField(default=0),
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
                "verbose_name": "Product",
                "verbose_name_plural": "Products",
            },
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(
                fields=["tenant", "name"],
                name="uq_product_tenant_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                check=models.Q(price__gte=0),
                name="ck_product_price_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                check=models.Q(currency__regex="^[A-Z]{3}$"),
                name="ck_product_currency_iso4217",
            ),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(
                fields=["tenant", "name"],
                name="ix_product_tenant_name",
            ),
        ),
    ]
