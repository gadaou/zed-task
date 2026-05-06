"""Migration: create InvoiceSequence and Invoice tables.

Implements PROJECT_SPEC §2 bonus: Invoice handling.
"""

import uuid
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("order", "0002_order_full_model"),
        ("tenant", "0001_initial"),
    ]

    operations = [
        # ------------------------------------------------------------------
        # 1. InvoiceSequence — per-tenant monotonic counter.
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name="InvoiceSequence",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
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
                ("last_number", models.PositiveIntegerField(default=0)),
            ],
            options={
                "verbose_name": "Invoice Sequence",
                "verbose_name_plural": "Invoice Sequences",
            },
        ),
        migrations.AddConstraint(
            model_name="invoicesequence",
            constraint=models.UniqueConstraint(
                fields=["tenant"],
                name="uq_invoicesequence_tenant",
            ),
        ),
        # ------------------------------------------------------------------
        # 2. Invoice — immutable financial document per paid order.
        # ------------------------------------------------------------------
        migrations.CreateModel(
            name="Invoice",
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
                    "tenant",
                    models.ForeignKey(
                        editable=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="tenant.tenant",
                    ),
                ),
                (
                    "order",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice",
                        to="order.order",
                    ),
                ),
                ("number", models.PositiveIntegerField()),
                (
                    "total",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                (
                    "taxes",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                ("currency", models.CharField(max_length=3)),
                ("pdf_url", models.CharField(max_length=512)),
                ("generated_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Invoice",
                "verbose_name_plural": "Invoices",
            },
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(
                fields=["tenant", "number"],
                name="uq_invoice_tenant_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_invoice_tenant_id",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=models.Q(total__gte=Decimal("0")),
                name="ck_invoice_total_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=models.Q(taxes__gte=Decimal("0")),
                name="ck_invoice_taxes_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=models.Q(number__gte=1),
                name="ck_invoice_number_pos",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=models.Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_invoice_currency_iso4217",
            ),
        ),
        migrations.AddIndex(
            model_name="invoice",
            index=models.Index(
                fields=["tenant", "order"],
                name="ix_invoice_tenant_order",
            ),
        ),
    ]
