from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from warehouse_analysis.history_db import (
    create_run,
    finish_run,
    initialize_database,
    query_history,
    save_error,
    save_warehouse_result,
)


class HistoryDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = self.root / "warehouse_analysis.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_01_database_and_four_tables_are_created(self) -> None:
        initialize_database(self.database)
        connection = sqlite3.connect(self.database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(
            {"run_history", "warehouse_result", "rate_snapshot", "error_log"}
            .issubset(tables)
        )

    def test_02_run_and_warehouse_result_are_saved(self) -> None:
        run_id = create_run(
            version="1.4.0",
            run_type="企业多仓批量分析",
            input_file="流水.xlsx",
            opening_file="期初.xlsx",
            start_date="2026-01-01",
            end_date="2026-01-31",
            path=self.database,
        )
        save_warehouse_result(
            run_id,
            warehouse="测试仓",
            period="2026-01",
            ending_inventory=100,
            average_inventory=90,
            average_days=20,
            fee=1260,
            report_path="reports/测试仓/报告.xlsx",
            path=self.database,
        )
        finish_run(run_id, "success", 2.5, self.database)
        records = query_history(path=self.database)
        self.assertEqual(records[0]["仓库名称"], "测试仓")
        self.assertEqual(records[0]["仓储费"], 1260)
        self.assertEqual(records[0]["运行状态"], "success")

    def test_03_history_filters_are_correct(self) -> None:
        run_id = create_run(
            version="1.4.0",
            run_type="企业多仓批量分析",
            input_file="流水.xlsx",
            opening_file="期初.xlsx",
            start_date="2026-02-01",
            end_date="2026-02-28",
            path=self.database,
        )
        save_warehouse_result(
            run_id,
            warehouse="上海仓",
            period="2026-02",
            ending_inventory=1,
            average_inventory=1,
            average_days=1,
            fee=1,
            report_path="report.xlsx",
            status="success",
            path=self.database,
        )
        finish_run(run_id, "success", 1, self.database)
        self.assertEqual(
            len(
                query_history(
                    warehouse="上海",
                    month="2026-02",
                    status="success",
                    path=self.database,
                )
            ),
            1,
        )
        self.assertEqual(
            len(query_history(warehouse="北京", path=self.database)), 0
        )

    def test_04_error_record_is_saved(self) -> None:
        save_error(
            warehouse="北京仓",
            module="费率匹配",
            error_type="MissingRate",
            description="未找到费率",
            suggestion="维护费率",
            path=self.database,
        )
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT 仓库, 错误类型, 解决建议 FROM error_log"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("北京仓", "MissingRate", "维护费率"))

    def test_05_corrupt_database_is_backed_up_and_recovered(self) -> None:
        self.database.write_bytes(b"not-a-sqlite-database")
        initialize_database(self.database)
        self.assertTrue(self.database.is_file())
        self.assertTrue(list(self.root.glob("*.corrupt-*.db")))
        connection = sqlite3.connect(self.database)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreaterEqual(count, 4)


if __name__ == "__main__":
    unittest.main()
