from __future__ import annotations

import unittest

import pandas as pd

from warehouse_analysis.core.calculation import run_engine
from warehouse_analysis.core.models import StatPeriod


TX_COLUMNS = ["仓库名称", "物料名称", "品类", "方向", "日期", "数量", "批次号"]
OPEN_COLUMNS = [
    "仓库名称", "物料名称", "品类", "期初数量",
    "期初批次入库基准日期", "期初批次号",
]
RATE_COLUMNS = [
    "仓库名称", "品类或物料", "单价", "计费方式说明", "适用层级", "生效日期",
]


def _frame(rows, columns):
    return pd.DataFrame(rows, columns=columns)


def _run(transactions, openings, rates=None, end="2025-01-31"):
    rates = rates or [["演示仓", "全部", 0.8, "演示费率", "全部", "2025-01-01"]]
    return run_engine(
        _frame(transactions, TX_COLUMNS),
        _frame(openings, OPEN_COLUMNS),
        _frame(rates, RATE_COLUMNS),
        [StatPeriod("测试期间", pd.Timestamp("2025-01-01"), pd.Timestamp(end))],
    )


class FifoEngineBoundaryTest(unittest.TestCase):
    def test_one_outbound_is_split_across_oldest_lots(self) -> None:
        result = _run(
            [
                ["演示仓", "材料A", "材料", "入库", "2025-01-03", 40, "IN-1"],
                ["演示仓", "材料A", "材料", "出库", "2025-01-10", 70, "OUT-1"],
            ],
            [["演示仓", "材料A", "材料", 50, "2025-01-01", "OPEN-1"]],
        )
        issued = result.batch_details[result.batch_details["出库批次号"] == "OUT-1"]
        self.assertEqual(issued["原始批次号"].tolist(), ["OPEN-1", "IN-1"])
        self.assertEqual(issued["数量（吨）"].tolist(), [50.0, 20.0])

    def test_shortage_rejects_the_whole_outbound_and_records_gap(self) -> None:
        result = _run(
            [["演示仓", "材料A", "材料", "出库", "2025-01-10", 60, "OUT-1"]],
            [["演示仓", "材料A", "材料", 50, "2025-01-01", "OPEN-1"]],
        )
        self.assertFalse((result.batch_details["状态"] == "已出库").any())
        self.assertEqual(float(result.anomalies.iloc[0]["差额"]), 10.0)
        self.assertIn("未分摊", result.anomalies.iloc[0]["异常原因"])

    def test_same_day_rows_follow_source_order(self) -> None:
        result = _run(
            [
                ["演示仓", "材料A", "材料", "入库", "2025-01-05", 10, "IN-1"],
                ["演示仓", "材料A", "材料", "出库", "2025-01-05", 10, "OUT-1"],
            ],
            [],
        )
        issued = result.batch_details[result.batch_details["状态"] == "已出库"]
        self.assertEqual(len(issued), 1)
        self.assertEqual(float(issued.iloc[0]["存放天数"]), 0.0)

    def test_same_day_outbound_before_inbound_is_rejected(self) -> None:
        result = _run(
            [
                ["演示仓", "材料A", "材料", "出库", "2025-01-05", 10, "OUT-1"],
                ["演示仓", "材料A", "材料", "入库", "2025-01-05", 10, "IN-1"],
            ],
            [],
        )
        self.assertFalse((result.batch_details["状态"] == "已出库").any())
        self.assertEqual(float(result.anomalies.iloc[0]["差额"]), 10.0)

    def test_storage_days_use_date_difference_without_plus_one(self) -> None:
        result = _run(
            [["演示仓", "材料A", "材料", "出库", "2025-01-06", 30, "OUT-1"]],
            [["演示仓", "材料A", "材料", 50, "2025-01-01", "OPEN-1"]],
        )
        issued = result.batch_details[result.batch_details["状态"] == "已出库"]
        self.assertEqual(float(issued.iloc[0]["存放天数"]), 5.0)
        self.assertEqual(float(issued.iloc[0]["仓储费（元）"]), 120.0)

    def test_rate_change_uses_outbound_date_for_issued_stock(self) -> None:
        result = _run(
            [["演示仓", "材料A", "材料", "出库", "2025-01-20", 10, "OUT-1"]],
            [["演示仓", "材料A", "材料", 20, "2025-01-01", "OPEN-1"]],
            rates=[
                ["演示仓", "全部", 0.5, "旧费率", "全部", "2025-01-01"],
                ["演示仓", "全部", 0.8, "新费率", "全部", "2025-01-15"],
            ],
        )
        issued = result.batch_details[result.batch_details["状态"] == "已出库"]
        self.assertEqual(float(issued.iloc[0]["单价（元/吨/天）"]), 0.8)

    def test_every_lot_passes_quantity_conservation(self) -> None:
        result = _run(
            [
                ["演示仓", "材料A", "材料", "入库", "2025-01-03", 40.125, "IN-1"],
                ["演示仓", "材料A", "材料", "出库", "2025-01-10", 60.375, "OUT-1"],
            ],
            [["演示仓", "材料A", "材料", 50.25, "2025-01-01", "OPEN-1"]],
        )
        self.assertEqual(set(result.conservation["校验结果"]), {"平衡"})
        self.assertTrue((result.conservation["数量差异（吨）"] == 0).all())


if __name__ == "__main__":
    unittest.main()
