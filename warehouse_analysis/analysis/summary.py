from __future__ import annotations

from decimal import Decimal

import pandas as pd

from warehouse_analysis.core.fifo_engine import _rebuild_fifo
from warehouse_analysis.core.models import EngineResult, StatPeriod
from warehouse_analysis.core.validation import normalize_inputs


WAREHOUSE_SUMMARY_COLUMNS = [
    "仓库名称",
    "统计期间",
    "统计开始日期",
    "统计截止日期",
    "期初库存量（吨）",
    "期间入库量（吨）",
    "期间出库量（吨）",
    "期末库存量（吨）",
    "平均库存量（吨）",
    "平均存放天数",
    "平均仓储费率（元/吨/天）",
    "仓储费总额（元）",
]


def _month_end_points(period: StatPeriod) -> list[pd.Timestamp]:
    """生成统计期间内的月末时点，不改变FIFO计算结果。"""
    first_month_end = period.start.to_period("M").to_timestamp("M")
    points = list(pd.date_range(first_month_end, period.end, freq="ME"))
    if not points:
        points = [period.end]
    return [pd.Timestamp(point) for point in points]


def _summarize_status(frame: pd.DataFrame) -> dict[str, float]:
    """汇总已出库和仍在库状态。

    输入：FIFO批次明细DataFrame。
    输出：数量、重量加权天数和仓储费汇总字典。
    业务含义：分别呈现周转和期末库存状态。
    FIFO影响：只汇总已有明细，不改变FIFO数量、顺序或拆分。
    """
    issued = frame[frame["状态"] == "已出库"]
    current = frame[frame["状态"] == "仍在库"]
    issued_qty = float(issued["数量（吨）"].sum())
    current_qty = float(current["数量（吨）"].sum())
    return {
        "已出库总量（吨）": issued_qty,
        "已出库加权平均周转天数": (
            float(issued["加权存放量（吨·天）"].sum() / issued_qty)
            if issued_qty
            else 0.0
        ),
        "已出库仓储费（元）": float(issued["仓储费（元）"].sum()),
        "当前总库存量（吨）": current_qty,
        "仍在库加权平均库龄（天）": (
            float(current["加权存放量（吨·天）"].sum() / current_qty)
            if current_qty
            else 0.0
        ),
        "仍在库仓储费（元）": float(current["仓储费（元）"].sum()),
        "系统计算仓储费（元）": float(frame["仓储费（元）"].sum()),
    }


def build_warehouse_summary(
    result: EngineResult,
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
    rates: pd.DataFrame,
    periods: list[StatPeriod],
) -> pd.DataFrame:
    """生成仓库经营分析DataFrame。

    输入：
    - result：现有run_engine计算结果；
    - transactions/openings/rates：与该结果对应的原始三张输入表；
    - periods：与run_engine相同的统计期间列表。

    输出：
    每个仓库、每个统计期间一行的warehouse_summary DataFrame。

    业务含义：
    在不重新计算仓储费的前提下，组合期初余额、期间流量、期末库存、
    月末平均库存、重量加权存放天数和平均费率，形成经营分析视图。

    FIFO影响：
    不实现或修改FIFO算法。期初余额仅调用现有_rebuild_fifo重建统计
    开始时点；期末、平均库存和费用直接使用现有EngineResult。
    """
    tx, op, _ = normalize_inputs(transactions, openings, rates)
    period_map = {
        (
            period.name,
            pd.Timestamp(period.start),
            pd.Timestamp(period.end),
        ): period
        for period in periods
    }
    rows: list[dict[str, object]] = []

    totals = result.warehouse_total.sort_values(
        ["仓库名称", "统计截止日", "统计开始日", "统计期间"],
        kind="stable",
    )
    for _, total in totals.iterrows():
        warehouse = str(total["仓库名称"])
        period_key = (
            str(total["统计期间"]),
            pd.Timestamp(total["统计开始日"]),
            pd.Timestamp(total["统计截止日"]),
        )
        if period_key not in period_map:
            raise ValueError(
                "经营分析找不到对应统计期间："
                f"仓库={warehouse}，期间={period_key[0]}，"
                f"开始={period_key[1].date()}，截止={period_key[2].date()}"
            )
        period = period_map[period_key]
        start = period.start.normalize()
        end_exclusive = period.end.normalize() + pd.Timedelta(days=1)

        # 期初快照是统计开始时点的初始条件；排除开始日当天流水后，
        # 调用现有FIFO即可得到开始日前一日结转至期初的库存余额。
        pre_period_transactions = tx[tx["日期"] < start]
        opening_lots, _, _ = _rebuild_fifo(
            warehouse,
            pre_period_transactions,
            op,
            start,
        )
        opening_quantity = sum(
            (
                lot["剩余数量"]
                for lot in opening_lots
                if lot["剩余数量"] > 0
            ),
            Decimal("0"),
        )

        period_transactions = tx[
            (tx["仓库名称"] == warehouse)
            & (tx["日期"] >= start)
            & (tx["日期"] < end_exclusive)
        ]
        inbound_quantity = sum(
            period_transactions.loc[
                period_transactions["方向"] == "入库", "数量"
            ],
            Decimal("0"),
        )
        outbound_quantity = sum(
            period_transactions.loc[
                period_transactions["方向"] == "出库", "数量"
            ],
            Decimal("0"),
        )

        details = result.batch_details[
            (result.batch_details["仓库名称"] == warehouse)
            & (result.batch_details["统计期间"] == period.name)
            & (result.batch_details["统计开始日"] == period.start)
            & (result.batch_details["统计截止日"] == period.end)
        ]
        weighted_storage = float(
            details["加权存放量（吨·天）"].sum()
        )
        detail_quantity = float(details["数量（吨）"].sum())
        average_days = (
            weighted_storage / detail_quantity if detail_quantity else 0.0
        )
        total_fee = float(total["系统计算仓储费（元）"])
        average_rate = (
            total_fee / weighted_storage if weighted_storage else 0.0
        )

        rows.append(
            {
                "仓库名称": warehouse,
                "统计期间": period.name,
                "统计开始日期": period.start,
                "统计截止日期": period.end,
                "期初库存量（吨）": float(opening_quantity),
                "期间入库量（吨）": float(inbound_quantity),
                "期间出库量（吨）": float(outbound_quantity),
                "期末库存量（吨）": float(total["当前总库存量（吨）"]),
                "平均库存量（吨）": float(
                    total["期间平均库存量（按月移动平均，吨）"]
                ),
                "平均存放天数": average_days,
                "平均仓储费率（元/吨/天）": average_rate,
                "仓储费总额（元）": total_fee,
            }
        )

    return pd.DataFrame(rows, columns=WAREHOUSE_SUMMARY_COLUMNS)
