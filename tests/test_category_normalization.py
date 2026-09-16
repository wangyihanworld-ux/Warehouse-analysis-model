from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from warehouse_analysis.io.category_normalizer import standardize_category_parts
from warehouse_analysis.io.excel_loader import load_transaction_file


class CategoryNormalizationTest(unittest.TestCase):
    def test_three_levels_use_erp_separator(self) -> None:
        self.assertEqual(
            standardize_category_parts(["聚乙烯", "LDPE", "薄膜料"]),
            "聚乙烯/LDPE/薄膜料",
        )

    def test_independent_file_reuses_three_level_standardization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            frame = pd.DataFrame(
                [
                    [
                        "测试仓库",
                        "物料A",
                        "聚乙烯",
                        "LDPE",
                        "薄膜料",
                        "入库",
                        "2025-01-01",
                        1,
                    ]
                ],
                columns=[
                    "仓库名称",
                    "物料名称",
                    "大类名称",
                    "中类名称",
                    "小类名称",
                    "方向",
                    "日期",
                    "数量",
                ],
            )
            frame.to_excel(path, index=False)
            result = load_transaction_file(path)
            self.assertEqual(result.iloc[0]["品类"], "聚乙烯/LDPE/薄膜料")


if __name__ == "__main__":
    unittest.main()
