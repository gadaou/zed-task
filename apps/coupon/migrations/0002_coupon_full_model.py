# Hand-authored migration for apps/coupon — replaces the placeholder Coupon
# model with the full production schema.
# PROJECT_SPEC §2 (coupon operations), §3.2 (tenant isolation), §3.5 (Decimal
# money + ISO 4217), §6.2 (UUID PKs), §6.6 (backwards-compatible migrations).
#
# ADR-NOTE: All new columns that need a non-null default are added with
# preserve_default=False so the interim default is applied only to any existing
# placeholder rows (none expected in real environments) and then discarded —
# the same pattern used in apps/cart/migrations/0002_cart_full_model.py.
# The stub Coupon had only (id, code, tenant, created_at, updated_at) so every
# new field can be added directly at its target nullability.

from decimal import Decimal

from django.db import migrations, models
from django.db.models import F


class Migration(migrations.Migration):
    dependencies = [
        ("coupon", "0001_initial"),
        ("tenant", "0001_initial"),
    ]

    operations = [
        # ------------------------------------------------------------------
        # Step 1 — Widen code field from max_length=100 to max_length=64.
        # 64 chars covers all real-world coupon code conventions; the spec
        # column plan says 64.  Shortening a VARCHAR is safe on Postgres
        # (no table rewrite if existing data fits; we have none).
        # ------------------------------------------------------------------
        migrations.AlterField(
            model_name="coupon",
            name="code",
            field=models.CharField(max_length=64),
        ),
        # ------------------------------------------------------------------
        # Step 2 — Add new Coupon fields.
        # ------------------------------------------------------------------
        migrations.AddField(
            model_name="coupon",
            name="discount_type",
            field=models.CharField(
                choices=[("PERCENTAGE", "Percentage"), ("FIXED", "Fixed amount")],
                default="PERCENTAGE",
                max_length=12,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="coupon",
            name="value",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("1.00"),
                max_digits=12,
            ),
            preserve_default=False,
        ),
        # nullable; PERCENTAGE coupons carry no currency
        migrations.AddField(
            model_name="coupon",
            name="currency",
            field=models.CharField(
                blank=True,
                default=None,
                max_length=3,
                null=True,
            ),
        ),
        # opaque constraint bag — default empty dict
        migrations.AddField(
            model_name="coupon",
            name="constraints",
            field=models.JSONField(default=dict),
        ),
        # null = unlimited
        migrations.AddField(
            model_name="coupon",
            name="usage_limit",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="coupon",
            name="used_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="coupon",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        # validity window — both nullable
        migrations.AddField(
            model_name="coupon",
            name="starts_at",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="coupon",
            name="ends_at",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        # ------------------------------------------------------------------
        # Step 3 — Constraints.
        # ------------------------------------------------------------------
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.UniqueConstraint(
                fields=["tenant", "code"],
                name="uq_coupon_tenant_code",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.UniqueConstraint(
                fields=["tenant", "id"],
                name="uq_coupon_tenant_id",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                check=models.Q(discount_type__in=["PERCENTAGE", "FIXED"]),
                name="ck_coupon_type_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                check=models.Q(value__gt=Decimal("0")),
                name="ck_coupon_value_pos",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                check=models.Q(discount_type="FIXED")
                | models.Q(value__lte=Decimal("100")),
                name="ck_coupon_pct_lte_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                check=models.Q(usage_limit__isnull=True)
                | models.Q(used_count__lte=F("usage_limit")),
                name="ck_coupon_used_within_limit",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                check=models.Q(currency__isnull=True)
                | models.Q(currency__regex=r"^[A-Z]{3}$"),
                name="ck_coupon_currency_iso4217",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(discount_type="PERCENTAGE", currency__isnull=True)
                    | models.Q(discount_type="FIXED", currency__isnull=False)
                ),
                name="ck_coupon_fixed_requires_currency",
            ),
        ),
        migrations.AddConstraint(
            model_name="coupon",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(ends_at__isnull=True)
                    | models.Q(starts_at__isnull=True)
                    | models.Q(ends_at__gte=F("starts_at"))
                ),
                name="ck_coupon_window_valid",
            ),
        ),
        # ------------------------------------------------------------------
        # Step 4 — Indexes.
        # (tenant, code) lookup is covered by the uq_coupon_tenant_code unique
        # constraint index; no separate index is added for it.
        # ------------------------------------------------------------------
        migrations.AddIndex(
            model_name="coupon",
            index=models.Index(
                fields=["tenant", "is_active", "ends_at"],
                name="ix_coupon_tenant_active_ends",
            ),
        ),
        migrations.AddIndex(
            model_name="coupon",
            index=models.Index(
                fields=["tenant", "ends_at"],
                name="ix_coupon_tenant_ends",
            ),
        ),
    ]
