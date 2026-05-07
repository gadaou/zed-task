# Adds a PostgreSQL partial unique index that enforces at most one ACTIVE cart
# per (tenant, user) pair.  CHECKED_OUT carts are excluded from the index so
# a user's historical carts are not affected.
#
# Rationale: the application-layer get_or_create_active_cart() call is not
# sufficient on its own — two concurrent requests can both pass the GET phase
# of get_or_create before either issues the INSERT, resulting in two ACTIVE
# carts.  The DB constraint eliminates that race.
#
# Backwards compatible: no data is changed; the constraint is simply not
# satisfied if two ACTIVE carts for the same (tenant, user) already exist.
# Run a one-off cleanup query before applying if the live database has
# pre-existing duplicates (unlikely; only possible if the app ran without
# this constraint).

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("cart", "0004_cart_selected_address_payment_method"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="cart",
            constraint=models.UniqueConstraint(
                condition=Q(status="ACTIVE"),
                fields=["tenant", "user_id"],
                name="uq_cart_one_active_per_tenant_user",
            ),
        ),
    ]
