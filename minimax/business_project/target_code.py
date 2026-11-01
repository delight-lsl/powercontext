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

"""Pricing engine calculation implementation (updated by MiniMax SDD Agent)."""

from typing import Optional


def calculate_final_amount(
    order_amount: float,
    discount_rate: float,
    user_tier: str = "REGULAR",
    coupon_code: Optional[str] = None,
) -> float:
    """Calculate the final payable order amount adhering to SDD specifications.

    Complies with:
    - [SPEC-001] Base discount: order_amount * (1.0 - discount_rate)
    - [SPEC-002] Tier privileges: VIP (5% off), SVIP (10% off)
    - [SPEC-003] Coupon deduction: SAVE10 gives flat 10.0 deduction
    - [SPEC-004] Input validation: order_amount > 0 and 0.0 <= discount_rate <= 1.0
    - [SPEC-005] Transaction bounds: capped at 50000.00, floored at 0.01
    """
    # [SPEC-004] Input validation & bounds checking
    if order_amount <= 0:
        raise ValueError("Order amount must be positive")
    if discount_rate < 0.0 or discount_rate > 1.0:
        raise ValueError("Invalid discount rate")

    # [SPEC-001] Base discount calculation
    amount = order_amount * (1.0 - discount_rate)

    # [SPEC-002] User tier discount
    if user_tier == "VIP":
        amount *= 0.95
    elif user_tier == "SVIP":
        amount *= 0.90

    # [SPEC-003] Coupon deduction
    if coupon_code == "SAVE10":
        amount -= 10.0

    # [SPEC-005] Bounds: cap maximum at 50,000.00 and floor minimum at 0.01
    if amount > 50000.0:
        amount = 50000.0
    if amount < 0.01:
        amount = 0.01

    return round(amount, 2)
