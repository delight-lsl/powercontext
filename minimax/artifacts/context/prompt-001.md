# Task

为发票总价增加促销码折扣能力，先折扣后计税


# Applicable specification

## SPEC-BILL-001 背景 (Background) (specs/SPEC-BILL-001-invoice-promo-discount.md:28-35) [source_id=sddctx:v1:spec_section:SPEC-BILL-001#背景-background:5d4baa04af01 db_position=1 sha256=d501c0167c7e server_digest=sha256:33addc17b6bc…]


当前 `billing.pricing.compute_invoice_totals` 只能计算"小计 + 税"的发票总价，
没有促销码能力。商务侧要求在开票时允许客户使用促销码抵扣，并在发票上体现
折扣行。本规格定义该能力的新行为。

新行为之外的所有既有行为（见 `SPEC-BILL-002`）必须保持不变。

## SPEC-BILL-001 折扣计算规则 (Discount rules) (specs/SPEC-BILL-001-invoice-promo-discount.md:56-67) [source_id=sddctx:v1:spec_section:SPEC-BILL-001#折扣计算规则-discount-rules:7a974a0a7887 db_position=4 sha256=d501c0167c7e server_digest=sha256:198721ea340a…]


设 `subtotal` 为小计金额，`p` 为促销码对应的 `percent_off` 与 `max_discount`：

1. 原始折扣 `raw = subtotal * percent_off / 100`，按 `ROUND_HALF_UP` 保留两位小数。
2. 实际折扣 `discount = min(raw, max_discount, subtotal)`。
3. 折扣金额不得为负，且 `discount <= subtotal`，因此 `total` 永远不小于 0。
4. 税基为 `subtotal - discount`，即**先折扣、后计税**。
5. `tax = round((subtotal - discount) * tax_rate, 2)`，同样使用 `ROUND_HALF_UP`。
6. `total = subtotal - discount + tax`。
7. `InvoiceTotals.discount` 必须返回实际折扣金额；无促销码时为 `0.00`。

## SPEC-BILL-001 不变量 (Invariants that must not change) (specs/SPEC-BILL-001-invoice-promo-discount.md:75-81) [source_id=sddctx:v1:spec_section:SPEC-BILL-001#不变量-invariants-that-must-not-change:d1825e319a76 db_position=6 sha256=d501c0167c7e server_digest=sha256:d2ea2a2925ee…]


- `subtotal` 的计算方式不变。
- 无促销码时 `discount == 0.00`，`tax == round(subtotal * tax_rate, 2)`。
- 金额一律使用 `Decimal` 与 `ROUND_HALF_UP`，禁止使用浮点数。
- 税率必须为 `Decimal` 且非负，否则抛出 `PricingError`。

## SPEC-BILL-001 促销码目录 (Promotion catalog) (specs/SPEC-BILL-001-invoice-promo-discount.md:46-55) [source_id=sddctx:v1:spec_section:SPEC-BILL-001#促销码目录-promotion-catalog:13bbead34177 db_position=3 sha256=d501c0167c7e server_digest=sha256:cd352b2f0c82…]


目录中的促销码语义如下，`percent_off` 为百分比，`max_discount` 为折扣上限金额：

| 促销码 | percent_off | max_discount | expires_on |
| --- | --- | --- | --- |
| `SAVE10` | 10 | 50.00 | 无 |
| `SAVE20` | 20 | 100.00 | 无 |
| `OLD15` | 15 | 30.00 | 2020-06-30（已过期） |

## SPEC-BILL-001 接口契约 (Interface contract) (specs/SPEC-BILL-001-invoice-promo-discount.md:36-45) [source_id=sddctx:v1:spec_section:SPEC-BILL-001#接口契约-interface-contract:f9be5a5c3bb1 db_position=2 sha256=d501c0167c7e server_digest=sha256:f14f1ef1c04c…]


- `compute_invoice_totals(items, *, tax_rate)` 必须继续可用且行为不变。
- `compute_invoice_totals` 新增可选关键字参数 `promo_code: str | None = None`。
  该参数是**关键字参数**，不得改变已有位置参数的含义。
- 新增异常类型，均继承自既有 `PricingError`：
  - `UnknownPromoError`：促销码不存在。
  - `ExpiredPromoError`：促销码已过期。
- 新增促销码目录 `PROMO_CATALOG`，为只读映射，键为促销码字符串。

## SPEC-BILL-001 示例 (Examples) (specs/SPEC-BILL-001-invoice-promo-discount.md:82-95) [source_id=sddctx:v1:spec_section:SPEC-BILL-001#示例-examples:ce7df2d7ffe8 db_position=7 sha256=d501c0167c7e server_digest=sha256:af1d1689ef0a…]


单件商品单价 `100.00`，税率 `10%`：

| 场景 | subtotal | discount | tax | total |
| --- | --- | --- | --- | --- |
| 无促销码 | 100.00 | 0.00 | 10.00 | 110.00 |
| `SAVE10` | 100.00 | 10.00 | 9.00 | 99.00 |

小计 `1000.00`，税率 `10%`，促销码 `SAVE10`（上限 50.00）：

| 场景 | subtotal | discount | tax | total |
| --- | --- | --- | --- | --- |
| `SAVE10`（触发上限） | 1000.00 | 50.00 | 95.00 | 1045.00 |

## SPEC-BILL-001 错误行为 (Error behaviour) (specs/SPEC-BILL-001-invoice-promo-discount.md:68-74) [source_id=sddctx:v1:spec_section:SPEC-BILL-001#错误行为-error-behaviour:c33c6c055d22 db_position=5 sha256=d501c0167c7e server_digest=sha256:bb2036ca3f3a…]


- `promo_code is None`：不应用任何折扣，行为与改动前完全一致。
- `promo_code` 不在 `PROMO_CATALOG` 中：抛出 `UnknownPromoError`。
- `promo_code` 已过期（`expires_on` 早于当前日期）：抛出 `ExpiredPromoError`。
- 错误必须在计算任何金额之前抛出，不得返回部分结果。

## SPEC-BILL-002 发票渲染 (Invoice rendering) (specs/SPEC-BILL-002-money-invariants.md:58-61) [source_id=sddctx:v1:spec_section:SPEC-BILL-002#发票渲染-invoice-rendering:1b54abbd9734 db_position=12 sha256=bff83dd0826d server_digest=sha256:22de93fb0ac8…]


- `render_invoice` 的输出必须逐行包含 `Subtotal:`、`Discount:`、`Tax:`、`Total:`。
- 在促销能力存在之前，`Discount:` 行渲染为 `0.00`。

## SPEC-BILL-002 计税 (Tax) (specs/SPEC-BILL-002-money-invariants.md:46-52) [source_id=sddctx:v1:spec_section:SPEC-BILL-002#计税-tax:b20ac6cda6c4 db_position=10 sha256=bff83dd0826d server_digest=sha256:37e871f1106a…]


- 税率必须为 `Decimal` 且非负，否则抛出 `PricingError`。
- 税 = `round(subtotal * tax_rate, 2)`，使用 `ROUND_HALF_UP`。
- 税基为小计（无折扣时）。
- `total = subtotal + tax`。

## SPEC-BILL-002 小计 (Subtotal) (specs/SPEC-BILL-002-money-invariants.md:40-45) [source_id=sddctx:v1:spec_section:SPEC-BILL-002#小计-subtotal:339da10d97b0 db_position=9 sha256=bff83dd0826d server_digest=sha256:8a22426ec035…]


- `compute_subtotal` 返回各行 `unit_price * quantity` 之和。
- `quantity` 必须为正整数，否则抛出 `PricingError`。
- 空订单的小计为 `0.00`。

## SPEC-BILL-002 舍入 (Rounding) (specs/SPEC-BILL-002-money-invariants.md:53-57) [source_id=sddctx:v1:spec_section:SPEC-BILL-002#舍入-rounding:a7e7b8948bcf db_position=11 sha256=bff83dd0826d server_digest=sha256:93e0c0215b44…]


- 采用"四舍五入"（`ROUND_HALF_UP`）而非银行家舍入（`ROUND_HALF_EVEN`）。
- 例如 `0.125 -> 0.13`，`0.135 -> 0.14`。

## SPEC-BILL-002 金额表示 (Money representation) (specs/SPEC-BILL-002-money-invariants.md:34-39) [source_id=sddctx:v1:spec_section:SPEC-BILL-002#金额表示-money-representation:07ced6879f83 db_position=8 sha256=bff83dd0826d server_digest=sha256:e689298737e7…]


- 所有金额使用 `decimal.Decimal`，禁止使用 `float`。
- `Money` 在构造时按 `ROUND_HALF_UP` 量化到两位小数。
- `Money` 不可变（frozen dataclass），且拒绝非有限值与非 `Decimal` 输入。


# Code to modify

## billing.InvoiceTotals (src/billing/pricing.py:49-55) [source_id=sddctx:v1:code_symbol:billing.InvoiceTotals:c7203165cefc db_position=19 sha256=6a69111712e4 server_digest=sha256:3e95dd799f31…]

```python
class InvoiceTotals:
    """The four monetary totals reported on an invoice."""

    subtotal: Money
    discount: Money
    tax: Money
    total: Money
```

## billing.LineItem (src/billing/pricing.py:30-45) [source_id=sddctx:v1:code_symbol:billing.LineItem:724def3ad26c db_position=18 sha256=6a69111712e4 server_digest=sha256:f0ac2307f5a8…]

```python
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
```

## billing.Money (src/billing/money.py:24-72) [source_id=sddctx:v1:code_symbol:billing.Money:920416fc29d2 db_position=16 sha256=45812cb90f24 server_digest=sha256:32aa8c402207…]

```python
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
```

## billing.MoneyError (src/billing/money.py:19-20) [source_id=sddctx:v1:code_symbol:billing.MoneyError:17291f2ed643 db_position=15 sha256=45812cb90f24 server_digest=sha256:db3fa17af942…]

```python
class MoneyError(ValueError):
    """Raised when a monetary value violates a project invariant."""
```

## billing.PricingError (src/billing/pricing.py:25-26) [source_id=sddctx:v1:code_symbol:billing.PricingError:0515c723287a db_position=17 sha256=6a69111712e4 server_digest=sha256:24690518c5d5…]

```python
class PricingError(ValueError):
    """Base class for pricing rule violations."""
```

## billing.compute_invoice_totals (src/billing/pricing.py:66-87) [source_id=sddctx:v1:code_symbol:billing.compute_invoice_totals:4da3b814217c db_position=21 sha256=6a69111712e4 server_digest=sha256:72efa14dffef…]

```python
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
```

## billing.compute_subtotal (src/billing/pricing.py:58-63) [source_id=sddctx:v1:code_symbol:billing.compute_subtotal:638f34b8b87c db_position=20 sha256=6a69111712e4 server_digest=sha256:90f3edea1547…]

```python
def compute_subtotal(items: tuple[LineItem, ...] | list[LineItem]) -> Money:
    """Return the sum of every line total, before tax and before any discount."""
    subtotal = Money.zero()
    for item in items:
        subtotal = subtotal + item.unit_price * item.quantity
    return subtotal
```


# Functional tests (must pass after the change)

## test_expired_promo_code_is_rejected (tests/test_pricing.py:120-122) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_expired_promo_code_is_rejected:54e94dcdbd14 db_position=46 sha256=c1aa66170083 server_digest=sha256:155abc4f98b2…]

```python
def test_expired_promo_code_is_rejected():
    with pytest.raises(pricing.ExpiredPromoError):
        pricing.compute_invoice_totals([_item()], tax_rate=Decimal("0.1"), promo_code="OLD15")
```

## test_promo_code_applies_a_percentage_discount_before_tax (tests/test_pricing.py:88-95) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_promo_code_applies_a_percentage_discount_before_tax:051ecad28f2e db_position=42 sha256=c1aa66170083 server_digest=sha256:db1cf9282fd1…]

```python
def test_promo_code_applies_a_percentage_discount_before_tax():
    totals = pricing.compute_invoice_totals(
        [_item()], tax_rate=Decimal("0.1"), promo_code="SAVE10"
    )
    assert totals.subtotal == Money.of("100.00")
    assert totals.discount == Money.of("10.00")
    assert totals.tax == Money.of("9.00")
    assert totals.total == Money.of("99.00")
```

## test_promo_discount_is_capped_by_the_maximum_discount (tests/test_pricing.py:98-104) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_promo_discount_is_capped_by_the_maximum_discount:78c6f02b998d db_position=43 sha256=c1aa66170083 server_digest=sha256:0e34866e645d…]

```python
def test_promo_discount_is_capped_by_the_maximum_discount():
    totals = pricing.compute_invoice_totals(
        [_item(unit_price="1000.00")], tax_rate=Decimal("0.1"), promo_code="SAVE10"
    )
    assert totals.discount == Money.of("50.00")
    assert totals.tax == Money.of("95.00")
    assert totals.total == Money.of("1045.00")
```

## test_promo_errors_are_pricing_errors (tests/test_pricing.py:125-127) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_promo_errors_are_pricing_errors:77c0c0b84ddd db_position=47 sha256=c1aa66170083 server_digest=sha256:51e231e98a89…]

```python
def test_promo_errors_are_pricing_errors():
    assert issubclass(pricing.UnknownPromoError, pricing.PricingError)
    assert issubclass(pricing.ExpiredPromoError, pricing.PricingError)
```

## test_promo_without_code_keeps_the_discount_at_zero (tests/test_pricing.py:107-112) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_promo_without_code_keeps_the_discount_at_zero:6632dd4c991f db_position=44 sha256=c1aa66170083 server_digest=sha256:e6a1e02177bd…]

```python
def test_promo_without_code_keeps_the_discount_at_zero():
    totals = pricing.compute_invoice_totals(
        [_item()], tax_rate=Decimal("0.1"), promo_code=None
    )
    assert totals.discount == Money.of("0.00")
    assert totals.total == Money.of("110.00")
```

## test_unknown_promo_code_is_rejected (tests/test_pricing.py:115-117) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_unknown_promo_code_is_rejected:02e990a601d6 db_position=45 sha256=c1aa66170083 server_digest=sha256:2e615bb9a60c…]

```python
def test_unknown_promo_code_is_rejected():
    with pytest.raises(pricing.UnknownPromoError):
        pricing.compute_invoice_totals([_item()], tax_rate=Decimal("0.1"), promo_code="NOPE")
```


# Regression tests (must keep passing)

## test_regression_invoice_keeps_the_invoice_identifier (tests/test_invoice.py:32-35) [source_id=sddctx:v1:test_case:tests/test_invoice.py::test_regression_invoice_keeps_the_invoice_identifier:ff79927be541 db_position=24 sha256=a20cf37f4cbf server_digest=sha256:0f6f6387d855…]

```python
def test_regression_invoice_keeps_the_invoice_identifier():
    totals = compute_invoice_totals(_items(), tax_rate=Decimal("0.0"))
    invoice = render_invoice("INV-2026-0003", _items(), totals)
    assert invoice.invoice_id == "INV-2026-0003"
```

## test_regression_invoice_lists_every_line_item (tests/test_invoice.py:26-29) [source_id=sddctx:v1:test_case:tests/test_invoice.py::test_regression_invoice_lists_every_line_item:3883ac29ce3a db_position=23 sha256=a20cf37f4cbf server_digest=sha256:e6f1df4c4549…]

```python
def test_regression_invoice_lists_every_line_item():
    totals = compute_invoice_totals(_items(), tax_rate=Decimal("0.0"))
    invoice = render_invoice("INV-2026-0002", _items(), totals)
    assert invoice.lines == ("SKU-1 x2 @ 19.99 = 39.98",)
```

## test_regression_invoice_renders_every_required_line (tests/test_invoice.py:16-23) [source_id=sddctx:v1:test_case:tests/test_invoice.py::test_regression_invoice_renders_every_required_line:b5ed3decd1c2 db_position=22 sha256=a20cf37f4cbf server_digest=sha256:03413f040761…]

```python
def test_regression_invoice_renders_every_required_line():
    totals = compute_invoice_totals(_items(), tax_rate=Decimal("0.13"))
    invoice = render_invoice("INV-2026-0001", _items(), totals)
    rendered = invoice.render()
    assert "Subtotal: 39.98" in rendered
    assert "Discount: 0.00" in rendered
    assert "Tax: 5.20" in rendered
    assert "Total: 45.18" in rendered
```

## test_regression_money_addition_and_subtraction (tests/test_money.py:27-29) [source_id=sddctx:v1:test_case:tests/test_money.py::test_regression_money_addition_and_subtraction:c15ca5441635 db_position=28 sha256=cc4d512668e2 server_digest=sha256:e8656e4dcf29…]

```python
def test_regression_money_addition_and_subtraction():
    assert Money.of("10.00") + Money.of("2.50") == Money.of("12.50")
    assert Money.of("10.00") - Money.of("2.50") == Money.of("7.50")
```

## test_regression_money_rejects_non_decimal_amount (tests/test_money.py:17-19) [source_id=sddctx:v1:test_case:tests/test_money.py::test_regression_money_rejects_non_decimal_amount:0f6ee07d97bd db_position=26 sha256=cc4d512668e2 server_digest=sha256:bd63638fe356…]

```python
def test_regression_money_rejects_non_decimal_amount():
    with pytest.raises(MoneyError):
        Money(1.5)  # type: ignore[arg-type]
```

## test_regression_money_rejects_non_finite_amount (tests/test_money.py:22-24) [source_id=sddctx:v1:test_case:tests/test_money.py::test_regression_money_rejects_non_finite_amount:f3eeb1b21be8 db_position=27 sha256=cc4d512668e2 server_digest=sha256:8504e0a11ea6…]

```python
def test_regression_money_rejects_non_finite_amount():
    with pytest.raises(MoneyError):
        Money(Decimal("NaN"))
```

## test_regression_money_rejects_non_integer_scaling (tests/test_money.py:36-38) [source_id=sddctx:v1:test_case:tests/test_money.py::test_regression_money_rejects_non_integer_scaling:a34706f8994f db_position=30 sha256=cc4d512668e2 server_digest=sha256:ebc759cababe…]

```python
def test_regression_money_rejects_non_integer_scaling():
    with pytest.raises(MoneyError):
        Money.of("1.00") * 1.5  # type: ignore[operator]
```

## test_regression_money_rounds_to_cents_with_half_up (tests/test_money.py:12-14) [source_id=sddctx:v1:test_case:tests/test_money.py::test_regression_money_rounds_to_cents_with_half_up:214b052af1a5 db_position=25 sha256=cc4d512668e2 server_digest=sha256:bec67acfa49a…]

```python
def test_regression_money_rounds_to_cents_with_half_up():
    assert Money(Decimal("1.005")).amount == Decimal("1.01")
    assert Money(Decimal("1.004")).amount == Decimal("1.00")
```

## test_regression_money_scaled_keeps_full_precision_until_quantize (tests/test_money.py:41-42) [source_id=sddctx:v1:test_case:tests/test_money.py::test_regression_money_scaled_keeps_full_precision_until_quantize:1f1e19a3ff3d db_position=31 sha256=cc4d512668e2 server_digest=sha256:6c00385fb2e5…]

```python
def test_regression_money_scaled_keeps_full_precision_until_quantize():
    assert Money.of("10.00").scaled(Decimal("0.075")).amount == Decimal("0.75")
```

## test_regression_money_scales_by_integer_quantity (tests/test_money.py:32-33) [source_id=sddctx:v1:test_case:tests/test_money.py::test_regression_money_scales_by_integer_quantity:9573ae6e7365 db_position=29 sha256=cc4d512668e2 server_digest=sha256:6f486f8a3e85…]

```python
def test_regression_money_scales_by_integer_quantity():
    assert Money.of("3.33") * 3 == Money.of("9.99")
```

## test_regression_money_zero_and_string_form (tests/test_money.py:45-47) [source_id=sddctx:v1:test_case:tests/test_money.py::test_regression_money_zero_and_string_form:ff2b1c5033c5 db_position=32 sha256=cc4d512668e2 server_digest=sha256:544266080725…]

```python
def test_regression_money_zero_and_string_form():
    assert Money.zero().is_zero()
    assert str(Money.of("3.5")) == "3.50"
```

## test_regression_empty_order_has_zero_subtotal (tests/test_pricing.py:38-39) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_empty_order_has_zero_subtotal:006441ae018c db_position=34 sha256=c1aa66170083 server_digest=sha256:f303e5617ff4…]

```python
def test_regression_empty_order_has_zero_subtotal():
    assert pricing.compute_subtotal([]) == Money.of("0.00")
```

## test_regression_line_item_requires_a_sku (tests/test_pricing.py:78-80) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_line_item_requires_a_sku:58a61a369d0d db_position=41 sha256=c1aa66170083 server_digest=sha256:ecedf62c304e…]

```python
def test_regression_line_item_requires_a_sku():
    with pytest.raises(pricing.PricingError):
        pricing.LineItem(sku="", quantity=1, unit_price=Money.of("1.00"))
```

## test_regression_negative_tax_rate_is_rejected (tests/test_pricing.py:68-70) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_negative_tax_rate_is_rejected:13f862bbc28a db_position=39 sha256=c1aa66170083 server_digest=sha256:95ef152eb710…]

```python
def test_regression_negative_tax_rate_is_rejected():
    with pytest.raises(pricing.PricingError):
        pricing.compute_invoice_totals([_item()], tax_rate=Decimal("-0.01"))
```

## test_regression_non_decimal_tax_rate_is_rejected (tests/test_pricing.py:63-65) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_non_decimal_tax_rate_is_rejected:c245b5fed30b db_position=38 sha256=c1aa66170083 server_digest=sha256:e3a2a0a53802…]

```python
def test_regression_non_decimal_tax_rate_is_rejected():
    with pytest.raises(pricing.PricingError):
        pricing.compute_invoice_totals([_item()], tax_rate=0.1)  # type: ignore[arg-type]
```

## test_regression_non_positive_quantity_is_rejected (tests/test_pricing.py:73-75) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_non_positive_quantity_is_rejected:e8228640fef7 db_position=40 sha256=c1aa66170083 server_digest=sha256:d9e64aa34d6b…]

```python
def test_regression_non_positive_quantity_is_rejected():
    with pytest.raises(pricing.PricingError):
        pricing.LineItem(sku="SKU-1", quantity=0, unit_price=Money.of("1.00"))
```

## test_regression_subtotal_is_the_sum_of_line_totals (tests/test_pricing.py:33-35) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_subtotal_is_the_sum_of_line_totals:d7e69e4c460c db_position=33 sha256=c1aa66170083 server_digest=sha256:f9742381d1ff…]

```python
def test_regression_subtotal_is_the_sum_of_line_totals():
    items = [_item(quantity=2, unit_price="19.99"), _item(sku="SKU-2", quantity=3, unit_price="5.00")]
    assert pricing.compute_subtotal(items) == Money.of("54.98")
```

## test_regression_tax_is_computed_on_the_subtotal (tests/test_pricing.py:42-47) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_tax_is_computed_on_the_subtotal:c85c279d3dc1 db_position=35 sha256=c1aa66170083 server_digest=sha256:6204822b4227…]

```python
def test_regression_tax_is_computed_on_the_subtotal():
    totals = pricing.compute_invoice_totals([_item()], tax_rate=Decimal("0.1"))
    assert totals.subtotal == Money.of("100.00")
    assert totals.discount == Money.of("0.00")
    assert totals.tax == Money.of("10.00")
    assert totals.total == Money.of("110.00")
```

## test_regression_tax_uses_round_half_up_not_bankers_rounding (tests/test_pricing.py:57-60) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_tax_uses_round_half_up_not_bankers_rounding:cccca3da1ec1 db_position=37 sha256=c1aa66170083 server_digest=sha256:455a60a28546…]

```python
def test_regression_tax_uses_round_half_up_not_bankers_rounding():
    # 12.50 * 0.01 = 0.125 -> 0.13 under ROUND_HALF_UP, 0.12 under ROUND_HALF_EVEN.
    totals = pricing.compute_invoice_totals([_item(unit_price="12.50")], tax_rate=Decimal("0.01"))
    assert totals.tax == Money.of("0.13")
```

## test_regression_total_equals_subtotal_plus_tax (tests/test_pricing.py:50-54) [source_id=sddctx:v1:test_case:tests/test_pricing.py::test_regression_total_equals_subtotal_plus_tax:9d79f7464dea db_position=36 sha256=c1aa66170083 server_digest=sha256:ddd2f27f074a…]

```python
def test_regression_total_equals_subtotal_plus_tax():
    totals = pricing.compute_invoice_totals(
        [_item(quantity=3, unit_price="19.99")], tax_rate=Decimal("0.13")
    )
    assert totals.total == totals.subtotal + totals.tax
```
