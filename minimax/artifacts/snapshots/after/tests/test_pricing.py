"""Pricing tests for the billing sample.

The regression cases (``test_regression_*``) encode behaviour that exists today
and must survive the ``SPEC-BILL-001`` promotion-code change.  The remaining
cases are the functional acceptance tests of that specification and are expected
to fail until the feature is implemented.

``billing.pricing`` is imported as a module on purpose: the functional tests
refer to symbols that do not exist yet, and a module-level ``from ... import
UnknownPromoError`` would break collection of the whole file, hiding the
regression suite.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from billing import pricing
from billing.money import Money


def _item(sku: str = "SKU-1", quantity: int = 1, unit_price: str = "100.00") -> pricing.LineItem:
    return pricing.LineItem(sku=sku, quantity=quantity, unit_price=Money.of(unit_price))


# ---------------------------------------------------------------------------
# Regression suite: behaviour that exists today and must be preserved.
# ---------------------------------------------------------------------------


def test_regression_subtotal_is_the_sum_of_line_totals():
    items = [_item(quantity=2, unit_price="19.99"), _item(sku="SKU-2", quantity=3, unit_price="5.00")]
    assert pricing.compute_subtotal(items) == Money.of("54.98")


def test_regression_empty_order_has_zero_subtotal():
    assert pricing.compute_subtotal([]) == Money.of("0.00")


def test_regression_tax_is_computed_on_the_subtotal():
    totals = pricing.compute_invoice_totals([_item()], tax_rate=Decimal("0.1"))
    assert totals.subtotal == Money.of("100.00")
    assert totals.discount == Money.of("0.00")
    assert totals.tax == Money.of("10.00")
    assert totals.total == Money.of("110.00")


def test_regression_total_equals_subtotal_plus_tax():
    totals = pricing.compute_invoice_totals(
        [_item(quantity=3, unit_price="19.99")], tax_rate=Decimal("0.13")
    )
    assert totals.total == totals.subtotal + totals.tax


def test_regression_tax_uses_round_half_up_not_bankers_rounding():
    # 12.50 * 0.01 = 0.125 -> 0.13 under ROUND_HALF_UP, 0.12 under ROUND_HALF_EVEN.
    totals = pricing.compute_invoice_totals([_item(unit_price="12.50")], tax_rate=Decimal("0.01"))
    assert totals.tax == Money.of("0.13")


def test_regression_non_decimal_tax_rate_is_rejected():
    with pytest.raises(pricing.PricingError):
        pricing.compute_invoice_totals([_item()], tax_rate=0.1)  # type: ignore[arg-type]


def test_regression_negative_tax_rate_is_rejected():
    with pytest.raises(pricing.PricingError):
        pricing.compute_invoice_totals([_item()], tax_rate=Decimal("-0.01"))


def test_regression_non_positive_quantity_is_rejected():
    with pytest.raises(pricing.PricingError):
        pricing.LineItem(sku="SKU-1", quantity=0, unit_price=Money.of("1.00"))


def test_regression_line_item_requires_a_sku():
    with pytest.raises(pricing.PricingError):
        pricing.LineItem(sku="", quantity=1, unit_price=Money.of("1.00"))


# ---------------------------------------------------------------------------
# Functional suite: SPEC-BILL-001 promotion-code discount.
# ---------------------------------------------------------------------------


def test_promo_code_applies_a_percentage_discount_before_tax():
    totals = pricing.compute_invoice_totals(
        [_item()], tax_rate=Decimal("0.1"), promo_code="SAVE10"
    )
    assert totals.subtotal == Money.of("100.00")
    assert totals.discount == Money.of("10.00")
    assert totals.tax == Money.of("9.00")
    assert totals.total == Money.of("99.00")


def test_promo_discount_is_capped_by_the_maximum_discount():
    totals = pricing.compute_invoice_totals(
        [_item(unit_price="1000.00")], tax_rate=Decimal("0.1"), promo_code="SAVE10"
    )
    assert totals.discount == Money.of("50.00")
    assert totals.tax == Money.of("95.00")
    assert totals.total == Money.of("1045.00")


def test_promo_without_code_keeps_the_discount_at_zero():
    totals = pricing.compute_invoice_totals(
        [_item()], tax_rate=Decimal("0.1"), promo_code=None
    )
    assert totals.discount == Money.of("0.00")
    assert totals.total == Money.of("110.00")


def test_unknown_promo_code_is_rejected():
    with pytest.raises(pricing.UnknownPromoError):
        pricing.compute_invoice_totals([_item()], tax_rate=Decimal("0.1"), promo_code="NOPE")


def test_expired_promo_code_is_rejected():
    with pytest.raises(pricing.ExpiredPromoError):
        pricing.compute_invoice_totals([_item()], tax_rate=Decimal("0.1"), promo_code="OLD15")


def test_promo_errors_are_pricing_errors():
    assert issubclass(pricing.UnknownPromoError, pricing.PricingError)
    assert issubclass(pricing.ExpiredPromoError, pricing.PricingError)
