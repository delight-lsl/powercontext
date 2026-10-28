"""Decimal money primitives used by the billing sample.

The sample project deliberately keeps every monetary value in ``Decimal`` so
that rounding behaviour is explicit and reproducible.  The invariants enforced
here are part of the public contract of the project and are covered by the
regression suite in ``tests/test_money.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

__all__ = ["CENTS", "Money", "MoneyError"]

CENTS = Decimal("0.01")


class MoneyError(ValueError):
    """Raised when a monetary value violates a project invariant."""


@dataclass(frozen=True)
class Money:
    """An immutable monetary amount rounded to whole cents."""

    amount: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise MoneyError("money amount must be a Decimal")
        if not self.amount.is_finite():
            raise MoneyError("money amount must be finite")
        object.__setattr__(self, "amount", self.amount.quantize(CENTS, rounding=ROUND_HALF_UP))

    @classmethod
    def zero(cls) -> "Money":
        return cls(Decimal("0"))

    @classmethod
    def of(cls, value: str | int | Decimal) -> "Money":
        return cls(Decimal(str(value)))

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.amount + other.amount)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.amount - other.amount)

    def __mul__(self, factor: int) -> "Money":
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise MoneyError("money can only be scaled by an integer quantity")
        return Money(self.amount * factor)

    def scaled(self, ratio: Decimal) -> "Money":
        """Return ``self * ratio`` without intermediate rounding loss."""
        if not isinstance(ratio, Decimal):
            raise MoneyError("scaling ratio must be a Decimal")
        return Money(self.amount * ratio)

    def is_zero(self) -> bool:
        return self.amount == Decimal("0")

    def __str__(self) -> str:
        return f"{self.amount:.2f}"

    def __repr__(self) -> str:
        return f"Money({self.amount:.2f})"
