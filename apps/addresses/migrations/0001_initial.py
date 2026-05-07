# Hand-authored initial migration for apps/addresses.
# PROJECT_SPEC §2 op 6 (add address), §3.2 (tenant isolation),
# §6.2 (UUID PKs), §6.6 (backwards-compatible migrations).

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
            name="Address",
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
                # Loose customer reference — no FK to auth.User
                ("user_id", models.UUIDField()),
                # ISO 3166-1 alpha-2
                ("country", models.CharField(max_length=2)),
                ("city", models.CharField(max_length=120)),
                # Opaque free-form details (street, postal code, etc.)
                ("details", models.TextField()),
                # Optional customer label ("home", "office", …)
                ("label", models.CharField(blank=True, default="", max_length=50)),
                ("is_default", models.BooleanField(default=False)),
                # Soft-delete — NULL = live
                (
                    "deleted_at",
                    models.DateTimeField(
                        blank=True, db_index=True, default=None, null=True
                    ),
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
                "verbose_name": "Address",
                "verbose_name_plural": "Addresses",
            },
        ),
        # ------------------------------------------------------------------
        # Constraints
        # ------------------------------------------------------------------
        migrations.AddConstraint(
            model_name="address",
            constraint=models.CheckConstraint(
                check=models.Q(country__regex=r"^[A-Z]{2}$"),
                name="ck_address_country_iso3166",
            ),
        ),
        migrations.AddConstraint(
            model_name="address",
            constraint=models.CheckConstraint(
                check=~models.Q(city=""),
                name="ck_address_city_nonempty",
            ),
        ),
        migrations.AddConstraint(
            model_name="address",
            constraint=models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_address_tenant_id",
            ),
        ),
        # Partial unique — at most one live default per (tenant, user).
        # Requires Postgres 9.5+ (standard for this project per §4.8).
        migrations.AddConstraint(
            model_name="address",
            constraint=models.UniqueConstraint(
                fields=["tenant", "user_id"],
                condition=models.Q(is_default=True, deleted_at__isnull=True),
                name="uq_address_one_default_per_user",
            ),
        ),
        # ------------------------------------------------------------------
        # Indexes
        # ------------------------------------------------------------------
        migrations.AddIndex(
            model_name="address",
            index=models.Index(
                fields=["tenant", "user_id"],
                name="ix_address_tenant_user",
            ),
        ),
        migrations.AddIndex(
            model_name="address",
            index=models.Index(
                fields=["tenant", "user_id", "is_default"],
                name="ix_address_tenant_user_default",
            ),
        ),
        migrations.AddIndex(
            model_name="address",
            index=models.Index(
                fields=["tenant", "country"],
                name="ix_address_tenant_country",
            ),
        ),
    ]
