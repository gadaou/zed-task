"""Address service functions — add_address, set_default_address, soft_delete_address.

Implementation lands with the view / checkout iteration that wires up
PROJECT_SPEC §2 operation 6.  The module is present now to maintain
structural consistency across all domain apps.

Expected surface (illustrative; not yet implemented):

    def add_address(
        *,
        user_id: uuid.UUID,
        country: str,
        city: str,
        details: str,
        label: str = "",
        is_default: bool = False,
    ) -> Address: ...

    def set_default_address(address_id: uuid.UUID) -> Address: ...

    def soft_delete_address(address_id: uuid.UUID) -> None: ...
"""
