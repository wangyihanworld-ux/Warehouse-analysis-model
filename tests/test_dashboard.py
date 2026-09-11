import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path

from warehouse_analysis.dashboard import generate_dashboard_charts, load_dashboard_data
from warehouse_analysis.history_db import create_run, save_error, save_warehouse_result


def seed_database(path):
    run_id = create_run(
        version="1.5.0", run_type="多仓", input_file="input.xlsx",
        opening_file="opening.xlsx", start_date="2026-01-01",
        end_date="2026-01-31", path=path,
    )
    for warehouse, inventory, average, days, fee, report in [
        ("甲仓", 100, 80, 20, 1400, "a.xlsx"),
        ("乙仓", 300, 250, 40, 8400, "b.xlsx"),
    ]:
        save_warehouse_result(
            run_id, warehouse=warehouse, period="2026-01-01至2026-01-31",
            ending_inventory=inventory, average_inventory=average,
            average_days=days, fee=fee, report_path=report, path=path,
        )
    save_error(
        run_id=run_id, warehouse="乙仓", module="FIFO", error_type="库存不足",
        description="测试异常", suggestion="核对流水", path=path,
    )
    return "2026-01-01至2026-01-31"


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "history.db"
        self.period = seed_database(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_dashboard_metrics_and_rankings(self):
        data = load_dashboard_data(self.database, period=self.period)
        self.assertEqual(data.metrics["仓库数量"], 2)
        self.assertEqual(data.metrics["总库存"], 400)
        self.assertEqual(data.metrics["平均库存"], 330)
        self.assertEqual(data.metrics["总仓储费"], 9800)
        self.assertEqual(data.metrics["平均存放天数"], 35)
        self.assertEqual(data.warehouse_ranking.iloc[0]["仓库名称"], "乙仓")

    def test_dashboard_anomaly_classification(self):
        data = load_dashboard_data(self.database, period=self.period)
        self.assertEqual(data.metrics["异常数量"], 1)
        self.assertEqual(data.anomalies.iloc[0]["异常分类"], "FIFO异常")

    def test_dashboard_charts_are_generated(self):
        paths = generate_dashboard_charts(
            load_dashboard_data(self.database, period=self.period),
            self.root / "charts",
        )
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(path.is_file() and path.stat().st_size for path in paths.values()))

    def test_snapshot_is_created_automatically(self):
        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM dashboard_snapshot").fetchone()[0]
        self.assertEqual(count, 2)

    def test_empty_dashboard_is_safe(self):
        empty = self.root / "empty.db"
        data = load_dashboard_data(empty)
        self.assertEqual(data.metrics["仓库数量"], 0)
        self.assertTrue(data.warehouse_ranking.empty)

    def test_trend_groups_warehouses_by_period(self):
        data = load_dashboard_data(self.database, period=self.period)
        self.assertEqual(len(data.trend), 1)
        self.assertEqual(float(data.trend.iloc[0]["仓储费"]), 9800)

    def test_nonstandard_error_is_classified_as_other(self):
        run_id = create_run(
            version="1.5.0", run_type="多仓", input_file="x",
            opening_file="y", start_date="2026-01-01", end_date="2026-01-31",
            path=self.database,
        )
        save_error(
            run_id=run_id, warehouse="甲仓", module="网络", error_type="超时",
            description="服务不可用", suggestion="稍后重试", path=self.database,
        )
        data = load_dashboard_data(self.database, period=self.period)
        self.assertIn("其他异常", set(data.anomalies["异常分类"]))


if __name__ == "__main__":
    unittest.main()
