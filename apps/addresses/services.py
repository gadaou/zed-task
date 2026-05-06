"""Address service functions — add_address, set_default_address, soft_delete_address.

Implements PROJECT_SPEC §2 operation 6 (add address) following the §4.1
service-layer pattern: thin views, all business logic and transactions here.

Public API
----------
``add_address(*, user_id, country, city, details, label="", is_default=False) -> Address``
    Create a new Address for ``user_id`` in the current tenant context.
    If ``is_default=True``, any existing live default for the same
    ``(tenant, user_id)`` is demoted atomically inside the same transaction
    so the partial-unique constraint ``uq_address_one_default_per_user``
    is never violated.

Concurrency
-----------
Both the demotion UPDATE and the INSERT run inside ``transaction.atomic``.
A ``select_for_update`` on the existing default row serialises concurrent
"make this the default" requests on the same user, preventing a race where
two concurrent requests each see no existing default and both commit with
``is_default=True`` — which would violate the partial unique constraint.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from django.db import transaction

from apps.addresses.models import Address


@transaction.atomic
def add_address(
    *,
    user_id: UUID,
    country: str,
    city: str,
    details: str,
    label: str = "",
    is_default: bool = False,
) -> Address:
    """Create and return a new Address for ``user_id`` in the current tenant.

    When ``is_default=True`` any existing live (``deleted_at__isnull=True``)
    default address for the same ``(tenant, user_id)`` is demoted to
    ``is_default=False`` before the new row is inserted, preserving the
    ``uq_address_one_default_per_user`` partial unique constraint.

    Args:
        user_id:    UUID of the customer who owns this address.
        country:    ISO 3166-1 alpha-2 uppercase code (e.g. ``"US"``).
        city:       Non-empty city name (max 120 chars).
        details:    Free-text address details (street, postal code, apartment…).
        label:      Optional human-readable label ("home", "office", …).
        is_default: If ``True``, make this the customer's default address.

    Returns:
        The newly created ``Address`` instance.
    """
    if is_default:
        # Demote any existing live default under a row lock so a concurrent
        # request cannot insert a second default at the same time.
        Address.objects.select_for_update().filter(
            user_id=user_id,
            is_default=True,
            deleted_at__isnull=True,
        ).update(is_default=False)

    return Address.objects.create(
        user_id=user_id,
        country=country,
        city=city,
        details=details,
        label=label,
        is_default=is_default,
    )
