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

"""Pricing engine calculation implementation (initial snapshot before SDD task)."""

from typing import Optional


def calculate_final_amount(
    order_amount: float,
    discount_rate: float,
    user_tier: str = "REGULAR",
    coupon_code: Optional[str] = None,
) -> float:
    """Calculate the final payable order amount.

    Initial implementation: supports base discount, user tiers, and coupons.
    Pending specification: input validation (SPEC-004) and bounds (SPEC-005).
    """
    amount = order_amount * (1.0 - discount_rate)

    if user_tier == "VIP":
        amount *= 0.95
    elif user_tier == "SVIP":
        amount *= 0.90

    if coupon_code == "SAVE10":
        amount -= 10.0

    return round(amount, 2)
