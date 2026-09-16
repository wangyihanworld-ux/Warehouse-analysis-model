from __future__ import annotations

from collections.abc import Iterable, Mapping

import pandas as pd


COMPANY_SUMMARY_COLUMNS = [
    "仓库名称",
    "统计期间",
    "期末库存（吨）",
    "平均库存（吨）",
    "平均存放天数（天）",
    "平均费率（元/吨/天）",
    "仓储费（元）",
    "异常数量（条）",
    "仓储费排名",
    "库存排名",
    "库龄排名",
]


def build_company_summary(
    warehouse_records: Iterable[Mapping[str, object]],
) -> pd.DataFrame:
    """汇总已有单仓服务结果并生成三项企业排名。

    输入为调度层收集的单仓结果，不调用FIFO、不重新计算单仓指标。
    """
    rows: list[dict[str, object]] = []
    for record in warehouse_records:
        if record.get("status") != "success":
            continue
        summary = record.get("summary", {})
        if not isinstance(summary, Mapping):
            continue
        rows.append(
            {
                "仓库名称": record.get("warehouse", ""),
                "统计期间": summary.get(
                    "统计期间", record.get("period", "")
                ),
                "期末库存（吨）": float(
                    summary.get("期末库存量（吨）", 0)
                ),
                "平均库存（吨）": float(
                    summary.get("平均库存量（吨）", 0)
                ),
                "平均存放天数（天）": float(
                    summary.get("平均存放天数", 0)
                ),
                "平均费率（元/吨/天）": float(
                    summary.get("平均仓储费率（元/吨/天）", 0)
                ),
                "仓储费（元）": float(
                    summary.get("仓储费总额（元）", 0)
                ),
                "异常数量（条）": int(
                    summary.get("异常记录数", 0)
                ),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=COMPANY_SUMMARY_COLUMNS)
    frame["仓储费排名"] = (
        frame["仓储费（元）"].rank(method="min", ascending=False).astype(int)
    )
    frame["库存排名"] = (
        frame["期末库存（吨）"].rank(method="min", ascending=False).astype(int)
    )
    frame["库龄排名"] = (
        frame["平均存放天数（天）"]
        .rank(method="min", ascending=False)
        .astype(int)
    )
    return (
        frame[COMPANY_SUMMARY_COLUMNS]
        .sort_values(["仓储费排名", "仓库名称"], kind="stable")
        .reset_index(drop=True)
    )
