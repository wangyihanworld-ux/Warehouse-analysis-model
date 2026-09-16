from __future__ import annotations

import unittest
from decimal import Decimal


def single_factor_contract(
    base_value: Decimal,
    base_fee: Decimal,
    change: Decimal,
) -> dict[str, Decimal]:
    """
    第五阶段敏感性模块必须遵守的数学契约。

    单因素模拟只改变一个驱动因素，不重新执行FIFO，其他因素保持不变。
    """
    factor = Decimal("1") + change
    simulated_value = base_value * factor
    simulated_fee = base_fee * factor
    fee_change = simulated_fee - base_fee
    fee_change_ratio = fee_change / base_fee if base_fee else Decimal("0")
    return {
        "模拟值": simulated_value,
        "模拟仓储费": simulated_fee,
        "费用变化金额": fee_change,
        "费用变化比例": fee_change_ratio,
    }


class SensitivityCalculationContractTest(unittest.TestCase):
    def test_inventory_scenarios(self) -> None:
        base_inventory = Decimal("3762.610")
        base_fee = Decimal("1095946.6427")
        changes = [
            Decimal("-0.10"),
            Decimal("-0.05"),
            Decimal("0"),
            Decimal("0.05"),
            Decimal("0.10"),
        ]

        for change in changes:
            with self.subTest(change=change):
                result = single_factor_contract(base_inventory, base_fee, change)
                self.assertEqual(
                    result["费用变化比例"],
                    change,
                )
                self.assertEqual(
                    result["模拟仓储费"],
                    base_fee * (Decimal("1") + change),
                )

    def test_storage_day_scenarios(self) -> None:
        base_days = Decimal("29.25234876854098")
        base_fee = Decimal("1095946.6427")

        for change in map(Decimal, ("-0.10", "-0.05", "0", "0.05", "0.10")):
            with self.subTest(change=change):
                result = single_factor_contract(base_days, base_fee, change)
                self.assertEqual(result["模拟值"], base_days * (Decimal("1") + change))
                self.assertEqual(result["费用变化比例"], change)

    def test_rate_scenarios(self) -> None:
        base_rate = Decimal("0.7")
        base_fee = Decimal("1095946.6427")

        for change in map(Decimal, ("-0.20", "-0.10", "0", "0.10", "0.20")):
            with self.subTest(change=change):
                result = single_factor_contract(base_rate, base_fee, change)
                self.assertEqual(result["模拟值"], base_rate * (Decimal("1") + change))
                self.assertEqual(result["费用变化金额"], base_fee * change)

    def test_zero_base_fee_is_safe(self) -> None:
        result = single_factor_contract(
            Decimal("100"),
            Decimal("0"),
            Decimal("0.10"),
        )
        self.assertEqual(result["费用变化比例"], Decimal("0"))


if __name__ == "__main__":
    unittest.main()
