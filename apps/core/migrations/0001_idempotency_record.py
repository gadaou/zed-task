"""Migration: create IdempotencyRecord table.

Implements PROJECT_SPEC §4.5 — durable idempotency store for the checkout
endpoint.  The table carries a unique index on (tenant_id, key) and a
CHECK constraint on the status column.
"""

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies: list = []

    operations = [
        migrations.CreateModel(
            name="IdempotencyRecord",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        default=uuid.uuid4,
                        editable=False,
                        serialize=False,
                    ),
                ),
                ("tenant_id", models.UUIDField(db_index=True)),
                ("key", models.UUIDField()),
                ("request_hash", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("IN_PROGRESS", "In progress"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                        ],
                        default="IN_PROGRESS",
                        max_length=12,
                    ),
                ),
                ("response_status", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("response_body", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Idempotency Record",
                "verbose_name_plural": "Idempotency Records",
            },
        ),
        migrations.AddConstraint(
            model_name="idempotencyrecord",
            constraint=models.UniqueConstraint(
                fields=["tenant_id", "key"],
                name="uq_idempotency_tenant_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="idempotencyrecord",
            constraint=models.CheckConstraint(
                check=models.Q(status__in=["IN_PROGRESS", "SUCCEEDED", "FAILED"]),
                name="ck_idempotency_status_valid",
            ),
        ),
        migrations.AddIndex(
            model_name="idempotencyrecord",
            index=models.Index(
                fields=["tenant_id", "key"],
                name="ix_idempotency_tenant_key",
            ),
        ),
        migrations.AddIndex(
            model_name="idempotencyrecord",
            index=models.Index(
                fields=["status", "created_at"],
                name="ix_idempotency_status_created",
            ),
        ),
    ]
