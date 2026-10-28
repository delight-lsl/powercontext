"""Regression tests for the money primitives (SPEC-BILL-002)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from billing.money import Money, MoneyError


def test_regression_money_rounds_to_cents_with_half_up():
    assert Money(Decimal("1.005")).amount == Decimal("1.01")
    assert Money(Decimal("1.004")).amount == Decimal("1.00")


def test_regression_money_rejects_non_decimal_amount():
    with pytest.raises(MoneyError):
        Money(1.5)  # type: ignore[arg-type]


def test_regression_money_rejects_non_finite_amount():
    with pytest.raises(MoneyError):
        Money(Decimal("NaN"))


def test_regression_money_addition_and_subtraction():
    assert Money.of("10.00") + Money.of("2.50") == Money.of("12.50")
    assert Money.of("10.00") - Money.of("2.50") == Money.of("7.50")


def test_regression_money_scales_by_integer_quantity():
    assert Money.of("3.33") * 3 == Money.of("9.99")


def test_regression_money_rejects_non_integer_scaling():
    with pytest.raises(MoneyError):
        Money.of("1.00") * 1.5  # type: ignore[operator]


def test_regression_money_scaled_keeps_full_precision_until_quantize():
    assert Money.of("10.00").scaled(Decimal("0.075")).amount == Decimal("0.75")


def test_regression_money_zero_and_string_form():
    assert Money.zero().is_zero()
    assert str(Money.of("3.5")) == "3.50"
