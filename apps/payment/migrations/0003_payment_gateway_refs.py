"""Add gateway_authorization_id and gateway_capture_id to Payment.

Implements PROJECT_SPEC §3.3 (pluggable gateway interface) and §6.6
(backwards-compatible schema changes: nullable/defaulted columns only).

Both columns are blank=True, default="" so existing Payment rows are
unaffected and old application pods can still read/write without error
during a rolling deployment.  They are populated by PaymentService when
the gateway returns a reference on success.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payment", "0002_payment"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="gateway_authorization_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Gateway-side authorization reference; populated on AUTHORIZED.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="gateway_capture_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Gateway-side capture reference; populated on CAPTURED.",
                max_length=255,
            ),
        ),
    ]
