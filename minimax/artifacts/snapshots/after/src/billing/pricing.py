"""Reference implementation of ``SPEC-BILL-001`` for ``src/billing/pricing.py``.

This file is **not** part of the sample project.  It is the hand written
reference solution of the promotion-code specification.  It exists for two
reasons:

* it proves that ``SPEC-BILL-001`` is implementable and that its acceptance
  tests are satisfiable, independently of any model availability;
* it gives the end-to-end script a deterministic "after" state when the
  MiniMax endpoint cannot be reached, so the before/after test evidence stays
  reproducible.

When MiniMax is reachable, the end-to-end script uses the model answer
instead and records which of the two sources produced the applied code.

Copy this file over ``sample/src/billing/pricing.py`` to obtain the "after"
state of the sample project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from typing import Mapping

from .money import Money, MoneyError

__all__ = [
    "ExpiredPromoError",
    "InvoiceTotals",
    "LineItem",
    "PROMO_CATALOG",
    "PricingError",
    "Promo",
    "UnknownPromoError",
    "compute_invoice_totals",
    "compute_subtotal",
]


class PricingError(ValueError):
    """Base class for pricing rule violations."""


class UnknownPromoError(PricingError):
    """Raised when the promotion code is not part of the catalog."""


class ExpiredPromoError(PricingError):
    """Raised when the promotion code is no longer valid."""


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


@dataclass(frozen=True)
class Promo:
    """One entry of the promotion code catalog."""

    percent_off: Decimal
    max_discount: Money
    expires_on: date | None = None

    def is_expired(self, today: date) -> bool:
        return self.expires_on is not None and self.expires_on < today


PROMO_CATALOG: Mapping[str, Promo] = MappingProxyType(
    {
        "SAVE10": Promo(percent_off=Decimal("10"), max_discount=Money.of("50.00")),
        "SAVE20": Promo(percent_off=Decimal("20"), max_discount=Money.of("100.00")),
        "OLD15": Promo(
            percent_off=Decimal("15"),
            max_discount=Money.of("30.00"),
            expires_on=date(2020, 6, 30),
        ),
    }
)


def compute_subtotal(items: tuple[LineItem, ...] | list[LineItem]) -> Money:
    """Return the sum of every line total, before tax and before any discount."""
    subtotal = Money.zero()
    for item in items:
        subtotal = subtotal + item.unit_price * item.quantity
    return subtotal


def _resolve_promo(promo_code: str) -> Promo:
    promo = PROMO_CATALOG.get(promo_code)
    if promo is None:
        raise UnknownPromoError(f"unknown promotion code: {promo_code}")
    if promo.is_expired(date.today()):
        raise ExpiredPromoError(f"promotion code expired: {promo_code}")
    return promo


def compute_invoice_totals(
    items: tuple[LineItem, ...] | list[LineItem],
    *,
    tax_rate: Decimal,
    promo_code: str | None = None,
) -> InvoiceTotals:
    """Return the invoice totals for ``items``.

    The tax is applied to the discounted subtotal and rounded to whole cents
    with ``ROUND_HALF_UP``.  Without a promotion code the behaviour is exactly
    the one of the pre-change implementation.
    """
    if not isinstance(tax_rate, Decimal):
        raise PricingError("tax rate must be a Decimal")
    if tax_rate < Decimal("0"):
        raise PricingError("tax rate must not be negative")

    promo = _resolve_promo(promo_code) if promo_code is not None else None

    subtotal = compute_subtotal(items)
    if promo is None:
        discount = Money.zero()
    else:
        raw = (subtotal.amount * promo.percent_off / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        capped = min(raw, promo.max_discount.amount, subtotal.amount)
        discount = Money(max(capped, Decimal("0")))

    tax_base = subtotal - discount
    tax = Money((tax_base.amount * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return InvoiceTotals(
        subtotal=subtotal,
        discount=discount,
        tax=tax,
        total=tax_base + tax,
    )
