"""Regression tests for invoice rendering (SPEC-BILL-002)."""

from __future__ import annotations

from decimal import Decimal

from billing.invoice import render_invoice
from billing.money import Money
from billing.pricing import LineItem, compute_invoice_totals


def _items() -> tuple[LineItem, ...]:
    return (LineItem(sku="SKU-1", quantity=2, unit_price=Money.of("19.99")),)


def test_regression_invoice_renders_every_required_line():
    totals = compute_invoice_totals(_items(), tax_rate=Decimal("0.13"))
    invoice = render_invoice("INV-2026-0001", _items(), totals)
    rendered = invoice.render()
    assert "Subtotal: 39.98" in rendered
    assert "Discount: 0.00" in rendered
    assert "Tax: 5.20" in rendered
    assert "Total: 45.18" in rendered


def test_regression_invoice_lists_every_line_item():
    totals = compute_invoice_totals(_items(), tax_rate=Decimal("0.0"))
    invoice = render_invoice("INV-2026-0002", _items(), totals)
    assert invoice.lines == ("SKU-1 x2 @ 19.99 = 39.98",)


def test_regression_invoice_keeps_the_invoice_identifier():
    totals = compute_invoice_totals(_items(), tax_rate=Decimal("0.0"))
    invoice = render_invoice("INV-2026-0003", _items(), totals)
    assert invoice.invoice_id == "INV-2026-0003"
