# Hand-authored migration for apps/payment — adds the Production Payment model
# alongside the existing PaymentMethod stub.
# PROJECT_SPEC §3.3 (pluggable gateways), §3.5 (Decimal money + ISO 4217),
# §5.3 (PaymentIntent FSM), §6.2 (UUID PKs), §6.6 (backwards-compatible).

import uuid
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payment", "0001_initial"),
        ("tenant", "0001_initial"),
        # Payment.cart FK — depends on the full Cart model being in place.
        ("cart", "0002_cart_full_model"),
    ]

    operations = [
        migrations.CreateModel(
            name="Payment",
            fields=[
                # TenantAwareModel base fields
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                # Primary key
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                # Cart FK — PROTECT: a cart with payments must not be dropped
                (
                    "cart",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payments",
                        to="cart.cart",
                    ),
                ),
                # Gateway slug from the PaymentGateway registry (§3.3)
                ("provider", models.CharField(max_length=50)),
                # FSM status (§5.3)
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("REQUIRES_CONFIRMATION", "Requires confirmation"),
                            ("AUTHORIZED", "Authorized"),
                            ("CAPTURED", "Captured"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                            ("REFUNDED", "Refunded"),
                        ],
                        default="REQUIRES_CONFIRMATION",
                        max_length=24,
                    ),
                ),
                # Money fields — always Decimal + ISO 4217 (§3.5)
                (
                    "amount",
                    models.DecimalField(decimal_places=2, max_digits=14),
                ),
                ("currency", models.CharField(max_length=3)),
                # Short gateway error reason on FAILED
                (
                    "failure_reason",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                # Tenant FK
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
                "verbose_name": "Payment",
                "verbose_name_plural": "Payments",
            },
        ),
        # ------------------------------------------------------------------
        # Constraints
        # ------------------------------------------------------------------
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                check=models.Q(amount__gte=Decimal("0")),
                name="ck_payment_amount_nonneg",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                check=models.Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_payment_currency_iso4217",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                check=models.Q(
                    status__in=[
                        "REQUIRES_CONFIRMATION",
                        "AUTHORIZED",
                        "CAPTURED",
                        "SUCCEEDED",
                        "FAILED",
                        "CANCELLED",
                        "REFUNDED",
                    ]
                ),
                name="ck_payment_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_payment_tenant_id",
            ),
        ),
        # ------------------------------------------------------------------
        # Indexes
        # ------------------------------------------------------------------
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["tenant", "cart"],
                name="ix_payment_tenant_cart",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["tenant", "status"],
                name="ix_payment_tenant_status",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["tenant", "provider", "status"],
                name="ix_payment_tenant_prov_status",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["tenant", "status", "updated_at"],
                name="ix_payment_tenant_status_upd",
            ),
        ),
    ]
