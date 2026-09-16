from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from warehouse_analysis.history_db import default_database_path, initialize_database


@dataclass(frozen=True)
class DashboardData:
    """经营驾驶舱只读数据，不参与任何核心计算。"""

    period: str
    metrics: dict[str, float | int | str]
    warehouse_ranking: pd.DataFrame
    trend: pd.DataFrame
    anomalies: pd.DataFrame


def _read_sql(connection: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, connection, params=params)


def load_dashboard_data(
    database_path: str | Path | None = None,
    *,
    period: str | None = None,
) -> DashboardData:
    """从V1.4历史库读取公司指标、排名、趋势和异常，不改写结果。"""
    database = initialize_database(database_path or default_database_path())
    with closing(sqlite3.connect(database)) as connection:
        if period is None:
            row = connection.execute(
                "SELECT MAX(期间) FROM dashboard_snapshot"
            ).fetchone()
            period = str(row[0] or "")
        ranking = _read_sql(
            connection,
            """
            SELECT 仓库 AS 仓库名称, 期间 AS 统计期间,
                   库存 AS 期末库存, 平均库存, 平均天数 AS 平均存放天数,
                   仓储费
            FROM dashboard_snapshot
            WHERE 期间=?
            ORDER BY 仓储费 DESC, 仓库
            """,
            (period,),
        )
        trend_raw = _read_sql(
            connection,
            """
            SELECT 期间, 仓库, 库存, 平均库存, 仓储费, 平均天数
            FROM dashboard_snapshot
            ORDER BY 期间, 仓库
            """,
        )
        anomaly_raw = _read_sql(
            connection,
            """
            SELECT e.时间, e.仓库, e.错误模块, e.错误类型,
                   e.错误描述, e.解决建议
            FROM error_log e
            LEFT JOIN run_history r ON r.id=e.run_id
            WHERE (?='' OR (r.开始日期 || '至' || r.结束日期)=?
                   OR r.开始日期 LIKE ?)
            ORDER BY e.时间 DESC
            """,
            (period, period, f"{period}%"),
        )

    if ranking.empty:
        metrics: dict[str, float | int | str] = {
            "统计期间": period,
            "仓库数量": 0,
            "总库存": 0.0,
            "平均库存": 0.0,
            "总仓储费": 0.0,
            "平均存放天数": 0.0,
            "异常数量": int(len(anomaly_raw)),
        }
    else:
        weights = ranking["期末库存"].clip(lower=0)
        avg_days = (
            float((ranking["平均存放天数"] * weights).sum() / weights.sum())
            if float(weights.sum()) != 0
            else float(ranking["平均存放天数"].mean())
        )
        metrics = {
            "统计期间": period,
            "仓库数量": int(ranking["仓库名称"].nunique()),
            "总库存": float(ranking["期末库存"].sum()),
            "平均库存": float(ranking["平均库存"].sum()),
            "总仓储费": float(ranking["仓储费"].sum()),
            "平均存放天数": avg_days,
            "异常数量": int(len(anomaly_raw)),
        }

    if trend_raw.empty:
        trend = pd.DataFrame(
            columns=["期间", "月末库存", "平均库存", "仓储费", "平均存放天数"]
        )
    else:
        grouped: list[dict[str, object]] = []
        for month, frame in trend_raw.groupby("期间", sort=True):
            weights = frame["库存"].clip(lower=0)
            days = (
                float((frame["平均天数"] * weights).sum() / weights.sum())
                if float(weights.sum()) != 0
                else float(frame["平均天数"].mean())
            )
            grouped.append(
                {
                    "期间": month,
                    "月末库存": float(frame["库存"].sum()),
                    "平均库存": float(frame["平均库存"].sum()),
                    "仓储费": float(frame["仓储费"].sum()),
                    "平均存放天数": days,
                }
            )
        trend = pd.DataFrame(grouped)

    if anomaly_raw.empty:
        anomalies = anomaly_raw.assign(异常分类=pd.Series(dtype=str))
    else:
        combined = anomaly_raw[
            ["错误模块", "错误类型", "错误描述"]
        ].fillna("").agg(" ".join, axis=1)

        def classify(value: str) -> str:
            if "FIFO" in value.upper():
                return "FIFO异常"
            if "费率" in value:
                return "缺少费率"
            if "人工" in value:
                return "人工核对规则"
            if "输入" in value or "字段" in value:
                return "输入错误"
            return "其他异常"

        anomalies = anomaly_raw.copy()
        anomalies.insert(0, "异常分类", combined.map(classify))
    return DashboardData(str(period), metrics, ranking, trend, anomalies)


def generate_dashboard_charts(
    data: DashboardData,
    output_directory: str | Path,
) -> dict[str, Path]:
    """生成TOP排名及趋势PNG；仅可视化现有数据库结果。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    paths: dict[str, Path] = {}
    measures = [
        ("仓储费", "仓储费TOP10", "top_fee.png"),
        ("期末库存", "库存TOP10", "top_inventory.png"),
        ("平均存放天数", "库龄TOP10", "top_age.png"),
    ]
    for field, title, filename in measures:
        frame = data.warehouse_ranking.nlargest(10, field).sort_values(field)
        fig, axis = plt.subplots(figsize=(8, 4.6))
        axis.barh(frame["仓库名称"], frame[field], color="#4472C4")
        axis.set_title(title)
        axis.set_xlabel(field)
        fig.tight_layout()
        path = output / filename
        fig.savefig(path, dpi=150)
        plt.close(fig)
        paths[title] = path

    fig, axes = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    axes[0].plot(data.trend["期间"], data.trend["仓储费"], marker="o")
    axes[0].set_title("仓储费趋势")
    axes[0].set_ylabel("元")
    axes[1].plot(
        data.trend["期间"], data.trend["月末库存"], marker="o", label="月末库存"
    )
    axes[1].plot(
        data.trend["期间"], data.trend["平均库存"], marker="o", label="平均库存"
    )
    axes[1].set_title("库存趋势")
    axes[1].set_ylabel("吨")
    axes[1].legend()
    axes[1].tick_params(axis="x", rotation=35)
    fig.tight_layout()
    trend_path = output / "monthly_trends.png"
    fig.savefig(trend_path, dpi=150)
    plt.close(fig)
    paths["趋势分析"] = trend_path
    return paths
