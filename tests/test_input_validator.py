from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from warehouse_analysis.io.input_validator import (
    InputValidationError,
    validate_input_files,
    validate_standardized_inputs,
)


def _valid_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    transactions = pd.DataFrame([{
        "仓库名称": "测试仓", "物料名称": "物料A", "品类": "树脂",
        "方向": "入库", "日期": "2025-01-02", "数量": 10, "批次号": "T1",
    }])
    openings = pd.DataFrame([{
        "仓库名称": "测试仓", "物料名称": "物料A", "品类": "树脂",
        "期初数量": 5, "期初批次入库基准日期": "2025-01-01", "期初批次号": "O1",
    }])
    return transactions, openings


class InputValidatorTest(unittest.TestCase):
    def test_missing_file_has_four_part_business_message(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "不存在.xlsx"
            with self.assertRaises(InputValidationError) as caught:
                validate_input_files(missing, None)
        message = str(caught.exception)
        for label in ("问题：", "位置：", "发现：", "建议："):
            self.assertIn(label, message)
        self.assertIn("文件不存在", message)

    def test_missing_field_is_reported_before_fifo(self) -> None:
        transactions, openings = _valid_inputs()
        openings = openings.drop(columns="期初数量")
        with self.assertRaises(InputValidationError) as caught:
            validate_standardized_inputs(transactions, openings)
        self.assertIn("缺少期初数量字段", str(caught.exception))

    def test_invalid_date_is_reported(self) -> None:
        transactions, openings = _valid_inputs()
        transactions.loc[0, "日期"] = "不是日期"
        with self.assertRaises(InputValidationError) as caught:
            validate_standardized_inputs(transactions, openings)
        self.assertIn("日期不合法", str(caught.exception))
        self.assertIn("无效日期", str(caught.exception))

    def test_invalid_quantity_is_reported(self) -> None:
        transactions, openings = _valid_inputs()
        openings["期初数量"] = openings["期初数量"].astype(object)
        openings.loc[0, "期初数量"] = "错误数量"
        with self.assertRaises(InputValidationError) as caught:
            validate_standardized_inputs(transactions, openings)
        self.assertIn("数量不合法", str(caught.exception))
        self.assertIn("非数字", str(caught.exception))

    def test_empty_opening_inventory_is_reported(self) -> None:
        transactions, openings = _valid_inputs()
        with self.assertRaises(InputValidationError) as caught:
            validate_standardized_inputs(transactions, openings.iloc[0:0])
        self.assertIn("期初库存为空", str(caught.exception))

    def test_valid_standardized_inputs_pass(self) -> None:
        validate_standardized_inputs(*_valid_inputs())


if __name__ == "__main__":
    unittest.main()
