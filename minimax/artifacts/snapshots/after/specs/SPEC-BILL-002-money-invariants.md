---
spec_id: SPEC-BILL-002
title: 金额与计税不变量 (Money and tax invariants)
status: approved
version: 1
applies_to:
  - symbol:billing.money.Money
  - symbol:billing.pricing.compute_subtotal
  - symbol:billing.pricing.compute_invoice_totals
  - symbol:billing.pricing.LineItem
  - file:src/billing/money.py
  - file:src/billing/pricing.py
  - test:tests/test_money.py
  - test:tests/test_pricing.py
  - test:tests/test_invoice.py
keywords:
  - 金额
  - 精度
  - 取整
  - 舍入
  - 不变量
  - money
  - rounding
  - decimal
  - tax
  - subtotal
---

# SPEC-BILL-002 金额与计税不变量

本规格记录**必须长期保持**的既有行为。任何针对 `billing` 的改动都不得破坏这些
不变量；`tests/` 中以 `test_regression_` 开头的用例即为这些不变量的可执行表达。

## 金额表示 (Money representation)

- 所有金额使用 `decimal.Decimal`，禁止使用 `float`。
- `Money` 在构造时按 `ROUND_HALF_UP` 量化到两位小数。
- `Money` 不可变（frozen dataclass），且拒绝非有限值与非 `Decimal` 输入。

## 小计 (Subtotal)

- `compute_subtotal` 返回各行 `unit_price * quantity` 之和。
- `quantity` 必须为正整数，否则抛出 `PricingError`。
- 空订单的小计为 `0.00`。

## 计税 (Tax)

- 税率必须为 `Decimal` 且非负，否则抛出 `PricingError`。
- 税 = `round(subtotal * tax_rate, 2)`，使用 `ROUND_HALF_UP`。
- 税基为小计（无折扣时）。
- `total = subtotal + tax`。

## 舍入 (Rounding)

- 采用"四舍五入"（`ROUND_HALF_UP`）而非银行家舍入（`ROUND_HALF_EVEN`）。
- 例如 `0.125 -> 0.13`，`0.135 -> 0.14`。

## 发票渲染 (Invoice rendering)

- `render_invoice` 的输出必须逐行包含 `Subtotal:`、`Discount:`、`Tax:`、`Total:`。
- 在促销能力存在之前，`Discount:` 行渲染为 `0.00`。
