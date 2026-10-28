---
spec_id: SPEC-BILL-001
title: 发票促销码折扣 (Invoice promotion-code discount)
status: approved
version: 1
applies_to:
  - symbol:billing.pricing.compute_invoice_totals
  - symbol:billing.pricing.InvoiceTotals
  - symbol:billing.pricing.PricingError
  - file:src/billing/pricing.py
  - test:tests/test_pricing.py
keywords:
  - 促销码
  - 折扣
  - 优惠
  - promo
  - discount
  - coupon
  - invoice
  - 发票
  - total
  - subtotal
  - tax
---

# SPEC-BILL-001 发票促销码折扣

## 背景 (Background)

当前 `billing.pricing.compute_invoice_totals` 只能计算"小计 + 税"的发票总价，
没有促销码能力。商务侧要求在开票时允许客户使用促销码抵扣，并在发票上体现
折扣行。本规格定义该能力的新行为。

新行为之外的所有既有行为（见 `SPEC-BILL-002`）必须保持不变。

## 接口契约 (Interface contract)

- `compute_invoice_totals(items, *, tax_rate)` 必须继续可用且行为不变。
- `compute_invoice_totals` 新增可选关键字参数 `promo_code: str | None = None`。
  该参数是**关键字参数**，不得改变已有位置参数的含义。
- 新增异常类型，均继承自既有 `PricingError`：
  - `UnknownPromoError`：促销码不存在。
  - `ExpiredPromoError`：促销码已过期。
- 新增促销码目录 `PROMO_CATALOG`，为只读映射，键为促销码字符串。

## 促销码目录 (Promotion catalog)

目录中的促销码语义如下，`percent_off` 为百分比，`max_discount` 为折扣上限金额：

| 促销码 | percent_off | max_discount | expires_on |
| --- | --- | --- | --- |
| `SAVE10` | 10 | 50.00 | 无 |
| `SAVE20` | 20 | 100.00 | 无 |
| `OLD15` | 15 | 30.00 | 2020-06-30（已过期） |

## 折扣计算规则 (Discount rules)

设 `subtotal` 为小计金额，`p` 为促销码对应的 `percent_off` 与 `max_discount`：

1. 原始折扣 `raw = subtotal * percent_off / 100`，按 `ROUND_HALF_UP` 保留两位小数。
2. 实际折扣 `discount = min(raw, max_discount, subtotal)`。
3. 折扣金额不得为负，且 `discount <= subtotal`，因此 `total` 永远不小于 0。
4. 税基为 `subtotal - discount`，即**先折扣、后计税**。
5. `tax = round((subtotal - discount) * tax_rate, 2)`，同样使用 `ROUND_HALF_UP`。
6. `total = subtotal - discount + tax`。
7. `InvoiceTotals.discount` 必须返回实际折扣金额；无促销码时为 `0.00`。

## 错误行为 (Error behaviour)

- `promo_code is None`：不应用任何折扣，行为与改动前完全一致。
- `promo_code` 不在 `PROMO_CATALOG` 中：抛出 `UnknownPromoError`。
- `promo_code` 已过期（`expires_on` 早于当前日期）：抛出 `ExpiredPromoError`。
- 错误必须在计算任何金额之前抛出，不得返回部分结果。

## 不变量 (Invariants that must not change)

- `subtotal` 的计算方式不变。
- 无促销码时 `discount == 0.00`，`tax == round(subtotal * tax_rate, 2)`。
- 金额一律使用 `Decimal` 与 `ROUND_HALF_UP`，禁止使用浮点数。
- 税率必须为 `Decimal` 且非负，否则抛出 `PricingError`。

## 示例 (Examples)

单件商品单价 `100.00`，税率 `10%`：

| 场景 | subtotal | discount | tax | total |
| --- | --- | --- | --- | --- |
| 无促销码 | 100.00 | 0.00 | 10.00 | 110.00 |
| `SAVE10` | 100.00 | 10.00 | 9.00 | 99.00 |

小计 `1000.00`，税率 `10%`，促销码 `SAVE10`（上限 50.00）：

| 场景 | subtotal | discount | tax | total |
| --- | --- | --- | --- | --- |
| `SAVE10`（触发上限） | 1000.00 | 50.00 | 95.00 | 1045.00 |
