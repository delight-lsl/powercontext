"""Invoice rendering for the billing sample.

``render_invoice`` is a pure consumer of :mod:`billing.pricing`.  It is not
touched by the ``SPEC-BILL-001`` change, which makes it a useful regression
guard: the rendered document must keep showing the discount line as zero
until the promotion feature exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from .money import Money
from .pricing import InvoiceTotals, LineItem

__all__ = ["Invoice", "render_invoice"]


@dataclass(frozen=True)
class Invoice:
    """A renderable invoice document."""

    invoice_id: str
    lines: tuple[str, ...]
    subtotal: str
    discount: str
    tax: str
    total: str

    def render(self) -> str:
        body = list(self.lines)
        body.append(f"Subtotal: {self.subtotal}")
        body.append(f"Discount: {self.discount}")
        body.append(f"Tax: {self.tax}")
        body.append(f"Total: {self.total}")
        return "\n".join(body)


def render_invoice(invoice_id: str, items: tuple[LineItem, ...], totals: InvoiceTotals) -> Invoice:
    """Render an invoice document from the already computed ``totals``."""
    lines = tuple(
        f"{item.sku} x{item.quantity} @ {item.unit_price} = {item.unit_price * item.quantity}"
        for item in items
    )
    return Invoice(
        invoice_id=invoice_id,
        lines=lines,
        subtotal=str(totals.subtotal),
        discount=str(totals.discount),
        tax=str(totals.tax),
        total=str(totals.total),
    )
