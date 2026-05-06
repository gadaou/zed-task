"""Invoice domain exceptions."""

from __future__ import annotations

from typing import Any


class InvoiceDomainError(Exception):
    """Base class for every invoice domain error."""

    type: str = "invoice/error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        self.detail = detail or self.__class__.__doc__ or ""
        self.extra = extra
        super().__init__(self.detail)


class OrderNotFound(InvoiceDomainError):
    """The requested order does not exist."""

    type = "invoice/order-not-found"


class OrderNotPaid(InvoiceDomainError):
    """Invoice generation requires the order to be in PAID status."""

    type = "invoice/order-not-paid"
