"""Line pricing and invoice totals for the billing sample.

This module is the target of the SDD change tracked by ``SPEC-BILL-001``:
the invoice total must support a promotional discount code.  The behaviour
that already exists here (subtotal computation, tax application and rounding)
must be preserved exactly; see ``SPEC-BILL-002`` for those invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .money import Money, MoneyError

__all__ = [
    "InvoiceTotals",
    "LineItem",
    "PricingError",
    "compute_invoice_totals",
    "compute_subtotal",
]


class PricingError(ValueError):
    """Base class for pricing rule violations."""


@dataclass(frozen=True)
class LineItem:
    """One priced order line."""

    sku: str
    quantity: int
    unit_price: Money

    def __post_init__(self) -> None:
        if not self.sku:
            raise PricingError("line item requires a sku")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool):
            raise PricingError("line item quantity must be an integer")
        if self.quantity <= 0:
            raise PricingError("line item quantity must be positive")
        if not isinstance(self.unit_price, Money):
            raise PricingError("line item unit price must be a Money instance")


@dataclass(frozen=True)
class InvoiceTotals:
    """The four monetary totals reported on an invoice."""

    subtotal: Money
    discount: Money
    tax: Money
    total: Money


def compute_subtotal(items: tuple[LineItem, ...] | list[LineItem]) -> Money:
    """Return the sum of every line total, before tax and before any discount."""
    subtotal = Money.zero()
    for item in items:
        subtotal = subtotal + item.unit_price * item.quantity
    return subtotal


def compute_invoice_totals(
    items: tuple[LineItem, ...] | list[LineItem],
    *,
    tax_rate: Decimal,
) -> InvoiceTotals:
    """Return the invoice totals for ``items``.

    The tax is applied to the subtotal and rounded to whole cents with
    ``ROUND_HALF_UP``.  No discount is applied by this function.
    """
    if not isinstance(tax_rate, Decimal):
        raise PricingError("tax rate must be a Decimal")
    if tax_rate < Decimal("0"):
        raise PricingError("tax rate must not be negative")
    subtotal = compute_subtotal(items)
    tax = Money((subtotal.amount * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return InvoiceTotals(
        subtotal=subtotal,
        discount=Money.zero(),
        tax=tax,
        total=subtotal + tax,
    )
