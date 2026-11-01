# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test suite for pricing calculation engine."""

import pytest

from .target_code import calculate_final_amount

# ==============================================================================
# 1. 现有基线行为回归测试 (修改前通过，修改后必须保持通过)
# ==============================================================================


def test_existing_base_discount() -> None:
    """验证 SPEC-001: 基础金额打折计算."""
    result = calculate_final_amount(order_amount=100.0, discount_rate=0.2)
    assert result == 80.0


def test_existing_vip_discount() -> None:
    """验证 SPEC-002: VIP与SVIP会员额外权益折让."""
    vip_result = calculate_final_amount(
        order_amount=200.0, discount_rate=0.1, user_tier="VIP"
    )
    # 200 * 0.9 * 0.95 = 171.0
    assert vip_result == 171.0

    svip_result = calculate_final_amount(
        order_amount=200.0, discount_rate=0.1, user_tier="SVIP"
    )
    # 200 * 0.9 * 0.90 = 162.0
    assert svip_result == 162.0


def test_existing_coupon_deduction() -> None:
    """验证 SPEC-003: SAVE10 优惠券立减10元机制."""
    result = calculate_final_amount(
        order_amount=100.0, discount_rate=0.0, coupon_code="SAVE10"
    )
    assert result == 90.0


# ==============================================================================
# 2. 目标新规格测试项 (修改前因未实现新需求而明确失败，修改后全部通过)
# ==============================================================================


def test_spec_004_negative_amount_raises_value_error() -> None:
    """验证 SPEC-004: 订单金额非法(<=0)时必须抛出 ValueError."""
    with pytest.raises(ValueError, match="Order amount must be positive"):
        calculate_final_amount(order_amount=-50.0, discount_rate=0.1)

    with pytest.raises(ValueError, match="Order amount must be positive"):
        calculate_final_amount(order_amount=0.0, discount_rate=0.1)


def test_spec_004_invalid_discount_rate_raises_value_error() -> None:
    """验证 SPEC-004: 折扣率超出 [0, 1] 时必须抛出 ValueError."""
    with pytest.raises(ValueError, match="Invalid discount rate"):
        calculate_final_amount(order_amount=100.0, discount_rate=1.5)

    with pytest.raises(ValueError, match="Invalid discount rate"):
        calculate_final_amount(order_amount=100.0, discount_rate=-0.2)


def test_spec_005_cap_maximum_amount() -> None:
    """验证 SPEC-005: 单笔最终应付金额封顶上限 50000.00 元."""
    # 60000 无折扣计算本应为 60000，受封顶保护应限制为 50000.00
    result = calculate_final_amount(order_amount=60000.0, discount_rate=0.0)
    assert result == 50000.0


def test_spec_005_floor_minimum_amount() -> None:
    """验证 SPEC-005: 订单金额为正时，立减抵扣后实付金额保底最低 0.01 元."""
    # 5.0 元使用 SAVE10 优惠券立减10元，不得得出负数或0，应保底为 0.01
    result = calculate_final_amount(
        order_amount=5.0, discount_rate=0.0, coupon_code="SAVE10"
    )
    assert result == 0.01
