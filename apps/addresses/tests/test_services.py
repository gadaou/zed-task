"""Unit tests for ``apps.addresses.services``.

Covers ``add_address``:
- Basic creation with all fields.
- ``is_default=True`` — demotes any existing live default for the same user.
- ``is_default=False`` — existing default is NOT touched.
- Multiple users in the same tenant do not interfere with each other's
  default demotion.
"""

from __future__ import annotations

import uuid

import pytest

from apps.addresses.models import Address
from apps.addresses.services import add_address
from apps.tenant.context import tenant_context
from apps.tenant.models import Tenant


@pytest.fixture
def tenant(db) -> Tenant:
    t = Tenant.objects.create(
        name="Addr Test Tenant",
        domain=f"addr-{uuid.uuid4().hex[:8]}.test",
    )
    with tenant_context(t):
        yield t


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.mark.django_db(transaction=True)
def test_add_address_creates_address(tenant, user_id) -> None:
    addr = add_address(
        user_id=user_id,
        country="US",
        city="Springfield",
        details="742 Evergreen Terrace",
    )
    assert addr.pk is not None
    assert addr.country == "US"
    assert addr.city == "Springfield"
    assert addr.is_default is False
    assert addr.deleted_at is None


@pytest.mark.django_db(transaction=True)
def test_add_address_with_label_and_is_default(tenant, user_id) -> None:
    addr = add_address(
        user_id=user_id,
        country="SA",
        city="Riyadh",
        details="King Fahd Road",
        label="office",
        is_default=True,
    )
    assert addr.label == "office"
    assert addr.is_default is True


@pytest.mark.django_db(transaction=True)
def test_add_address_is_default_demotes_existing(tenant, user_id) -> None:
    """Making a new address default demotes the previous default."""
    first = add_address(
        user_id=user_id,
        country="US",
        city="Springfield",
        details="1st St",
        is_default=True,
    )
    assert first.is_default is True

    second = add_address(
        user_id=user_id,
        country="US",
        city="Shelbyville",
        details="2nd Ave",
        is_default=True,
    )

    first.refresh_from_db()
    assert second.is_default is True
    assert first.is_default is False, "Previous default should have been demoted"


@pytest.mark.django_db(transaction=True)
def test_add_address_not_default_leaves_existing_default(tenant, user_id) -> None:
    """Adding a non-default address does not touch existing defaults."""
    first = add_address(
        user_id=user_id,
        country="US",
        city="Springfield",
        details="1st St",
        is_default=True,
    )

    second = add_address(
        user_id=user_id,
        country="US",
        city="Shelbyville",
        details="2nd Ave",
        is_default=False,
    )

    first.refresh_from_db()
    assert first.is_default is True, "Existing default should be unchanged"
    assert second.is_default is False


@pytest.mark.django_db(transaction=True)
def test_add_address_demotion_is_user_scoped(tenant) -> None:
    """Default demotion must not affect a different user's default address."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    addr_a = add_address(
        user_id=user_a, country="US", city="City A", details="St A", is_default=True
    )
    addr_b = add_address(
        user_id=user_b, country="US", city="City B", details="St B", is_default=True
    )

    # user_a adds a new default — should NOT demote user_b's address.
    add_address(
        user_id=user_a, country="US", city="City A2", details="St A2", is_default=True
    )

    addr_a.refresh_from_db()
    addr_b.refresh_from_db()
    assert addr_a.is_default is False, "user_a old default should be demoted"
    assert addr_b.is_default is True, "user_b default must be untouched"


@pytest.mark.django_db(transaction=True)
def test_add_address_soft_deleted_default_not_demoted(tenant, user_id) -> None:
    """A soft-deleted default is not a live default and must not be demoted by demotion logic
    (it's already out of the picture — the partial unique constraint only considers live rows)."""
    first = add_address(
        user_id=user_id,
        country="US",
        city="Old City",
        details="Old St",
        is_default=True,
    )
    from django.utils import timezone
    first.deleted_at = timezone.now()
    first.save(update_fields=["deleted_at"])

    # This should not raise a unique-constraint error.
    second = add_address(
        user_id=user_id,
        country="US",
        city="New City",
        details="New St",
        is_default=True,
    )
    assert second.is_default is True
