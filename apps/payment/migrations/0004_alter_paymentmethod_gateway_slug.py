"""Change PaymentMethod.gateway_slug default from 'mock' to 'dummy_success'.

Implements PROJECT_SPEC §3.3 naming alignment: the canonical slug for the
always-succeeding test gateway is 'dummy_success'.  The 'mock' slug is
retained as a registered alias in the gateway registry (apps/payment/gateways/
dummy.py) so existing rows with gateway_slug='mock' continue to resolve
without this migration affecting them.

Backwards-compatible: altering a column default changes only the Django ORM
default for new INSERTs.  Existing rows are untouched.  Old app pods running
against the new schema default to 'dummy_success' in Python code, which is
harmless — 'dummy_success' resolves correctly in both old and new pods.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payment", "0003_payment_gateway_refs"),
    ]

    operations = [
        migrations.AlterField(
            model_name="paymentmethod",
            name="gateway_slug",
            field=models.CharField(default="dummy_success", max_length=50),
        ),
    ]
