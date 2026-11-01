# 交易结算定价引擎系统规范 (SDD Specifications)

## 模块概述
本规范定义了电商/金融交易结算定价引擎核心函数 `calculate_final_amount` 的计算行为、用户等级折扣权益与输入边界约束。

---

## 规格条目清单

### [SPEC-001] 基础折扣计算逻辑 (Baseline Discount)
- **描述**: 系统根据传入的原始订单金额 `order_amount` 与基础折扣率 `discount_rate` 进行折算。
- **公式**: `base_amount = order_amount * (1.0 - discount_rate)`
- **精度**: 所有最终计算金额保留 2 位小数四舍五入。

### [SPEC-002] 会员等级权益折让 (Tier Privileges)
- **描述**: 支持不同会员等级在基础折扣后的阶梯叠加优惠：
  - `REGULAR`: 无额外优惠（倍率 1.0）。
  - `VIP`: 享受额外 5% 折扣（乘以 0.95）。
  - `SVIP`: 享受额外 10% 折扣（乘以 0.90）。

### [SPEC-003] 优惠券抵扣机制 (Coupon Deduction)
- **描述**: 支持优惠券代码核销：
  - 当 `coupon_code == "SAVE10"` 时，在会员折让后直接立减 10.00 元。
  - 未指定或空优惠券代码时不进行立减。

### [SPEC-004] 输入校验与异常保护 (Input Validation - Target Spec)
- **描述**:
  - `order_amount` 必须为正数。若 `order_amount <= 0`，必须显式抛出 `ValueError("Order amount must be positive")`。
  - `discount_rate` 必须介于 `[0.0, 1.0]` 区间。若 `discount_rate < 0.0` 或 `discount_rate > 1.0`，必须显式抛出 `ValueError("Invalid discount rate")`。

### [SPEC-005] 交易金额封顶与保底规则 (Transaction Bounds - Target Spec)
- **描述**:
  - **单笔封顶 (Cap)**: 经所有折扣及立减折算后的应付金额最高不超过 50,000.00 元。若计算值大于 50,000.00 元，应取 50,000.00 元。
  - **交易保底 (Floor)**: 当订单原始金额为正数时，扣除优惠券后的实付金额最低不能低于 0.01 元（避免产生负数金额或0元结算漏洞）。
