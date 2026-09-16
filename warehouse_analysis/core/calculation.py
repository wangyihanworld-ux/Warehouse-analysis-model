from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import pandas as pd

from warehouse_analysis.analysis.summary import (
    _month_end_points,
    _summarize_status,
)
from warehouse_analysis.core.fifo_engine import _rebuild_fifo
from warehouse_analysis.core.models import EngineResult, StatPeriod
from warehouse_analysis.core.rate_engine import RateResolver
from warehouse_analysis.core.validation import normalize_inputs


def run_engine(
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
    rates: pd.DataFrame,
    periods: list[StatPeriod],
) -> EngineResult:
    """运行多仓库、多期间FIFO仓储计算。

    输入：三张原始DataFrame和统计期间列表。
    输出：EngineResult中的批次、三级汇总、月末快照、守恒和异常结果。
    业务含义：连接输入校验、费率匹配、FIFO重建和汇总计算。
    FIFO影响：从黄金版本等价迁移，字段、排序、日期和金额口径不变。
    """
    tx, op, rt = normalize_inputs(transactions, openings, rates)
    warehouses = sorted(set(tx["仓库名称"]) | set(op["仓库名称"]))
    materials = set(tx["物料名称"]) | set(op["物料名称"])
    categories = set(tx["品类"]) | set(op["品类"])
    rate_resolver = RateResolver(rt, materials, categories)

    all_details = []
    all_material = []
    all_category = []
    all_warehouse = []
    all_monthly = []
    all_conservation = []
    all_anomalies = []

    for warehouse in warehouses:
        for period in sorted(periods, key=lambda p: (p.end, p.start, p.name)):
            lots, allocations, anomalies = _rebuild_fifo(
                warehouse, tx, op, period.end
            )
            lot_map = {lot["引擎批次ID"]: lot for lot in lots}
            issued_by_lot = defaultdict(lambda: Decimal("0"))
            detail_rows = []
            for allocation in allocations:
                lot = lot_map[allocation["引擎批次ID"]]
                issued_by_lot[lot["引擎批次ID"]] += allocation["出库数量"]
                rate, rate_note = rate_resolver.resolve(
                    warehouse,
                    lot["物料名称"],
                    lot["品类"],
                    allocation["出库日期"],
                )
                weighted = allocation["出库数量"] * Decimal(
                    allocation["存放天数"]
                )
                detail_rows.append(
                    {
                        "仓库名称": warehouse,
                        "统计期间": period.name,
                        "统计开始日": period.start,
                        "统计截止日": period.end,
                        "引擎批次ID": lot["引擎批次ID"],
                        "原始批次号": lot["原始批次号"],
                        "物料名称": lot["物料名称"],
                        "品类": lot["品类"],
                        "批次来源": lot["批次来源"],
                        "入库日期": lot["入库日期"],
                        "原始入库数量（吨）": float(lot["原始入库数量"]),
                        "状态": "已出库",
                        "出库日期": allocation["出库日期"],
                        "出库批次号": allocation["出库批次号"],
                        "数量（吨）": float(allocation["出库数量"]),
                        "存放天数": allocation["存放天数"],
                        "加权存放量（吨·天）": float(weighted),
                        "单价（元/吨/天）": float(rate),
                        "计费方式说明": rate_note,
                        "仓储费（元）": float(weighted * rate),
                    }
                )
            for lot in lots:
                if lot["剩余数量"] <= 0:
                    continue
                days = (
                    period.end.normalize() - lot["入库日期"].normalize()
                ).days
                rate, rate_note = rate_resolver.resolve(
                    warehouse,
                    lot["物料名称"],
                    lot["品类"],
                    period.end,
                )
                weighted = lot["剩余数量"] * Decimal(days)
                detail_rows.append(
                    {
                        "仓库名称": warehouse,
                        "统计期间": period.name,
                        "统计开始日": period.start,
                        "统计截止日": period.end,
                        "引擎批次ID": lot["引擎批次ID"],
                        "原始批次号": lot["原始批次号"],
                        "物料名称": lot["物料名称"],
                        "品类": lot["品类"],
                        "批次来源": lot["批次来源"],
                        "入库日期": lot["入库日期"],
                        "原始入库数量（吨）": float(lot["原始入库数量"]),
                        "状态": "仍在库",
                        "出库日期": None,
                        "出库批次号": None,
                        "数量（吨）": float(lot["剩余数量"]),
                        "存放天数": days,
                        "加权存放量（吨·天）": float(weighted),
                        "单价（元/吨/天）": float(rate),
                        "计费方式说明": rate_note,
                        "仓储费（元）": float(weighted * rate),
                    }
                )
            details = pd.DataFrame(detail_rows)
            if details.empty:
                continue
            details = details.sort_values(
                [
                    "物料名称",
                    "入库日期",
                    "引擎批次ID",
                    "状态",
                    "出库日期",
                ],
                kind="stable",
                na_position="last",
            )
            all_details.append(details)

            for lot in lots:
                issued = issued_by_lot[lot["引擎批次ID"]]
                difference = (
                    lot["原始入库数量"] - issued - lot["剩余数量"]
                )
                all_conservation.append(
                    {
                        "仓库名称": warehouse,
                        "统计期间": period.name,
                        "统计截止日": period.end,
                        "引擎批次ID": lot["引擎批次ID"],
                        "原始批次号": lot["原始批次号"],
                        "物料名称": lot["物料名称"],
                        "品类": lot["品类"],
                        "入库日期": lot["入库日期"],
                        "原始入库数量（吨）": float(lot["原始入库数量"]),
                        "已出库数量合计（吨）": float(issued),
                        "仍在库数量（吨）": float(lot["剩余数量"]),
                        "数量差异（吨）": float(difference),
                        "校验结果": "平衡" if difference == 0 else "不平衡",
                    }
                )
            for anomaly in anomalies:
                all_anomalies.append(
                    {
                        "统计期间": period.name,
                        "统计截止日": period.end,
                        **anomaly,
                    }
                )

            for material, group in details.groupby(
                "物料名称", sort=True
            ):
                row = {
                    "仓库名称": warehouse,
                    "统计期间": period.name,
                    "统计截止日": period.end,
                    "物料名称": material,
                    "品类": group["品类"].iloc[0],
                    **_summarize_status(group),
                }
                all_material.append(row)
            for category, group in details.groupby("品类", sort=True):
                all_category.append(
                    {
                        "仓库名称": warehouse,
                        "统计期间": period.name,
                        "统计截止日": period.end,
                        "品类": category,
                        **_summarize_status(group),
                    }
                )

            current = details[details["状态"] == "仍在库"]
            end_current_materials = set(current["物料名称"])
            month_points = _month_end_points(period)
            monthly_totals = []
            for point in month_points:
                point_lots, _, _ = _rebuild_fifo(
                    warehouse, tx, op, point
                )
                point_rows = [
                    {
                        "仓库名称": warehouse,
                        "统计期间": period.name,
                        "月份": point.strftime("%Y-%m"),
                        "月末日期": point,
                        "物料名称": lot["物料名称"],
                        "品类": lot["品类"],
                        "月末库存量（吨）": float(lot["剩余数量"]),
                    }
                    for lot in point_lots
                    if lot["剩余数量"] > 0
                    and lot["物料名称"] in end_current_materials
                ]
                monthly = pd.DataFrame(point_rows)
                if monthly.empty:
                    monthly_totals.append(0.0)
                    continue
                grouped = monthly.groupby(
                    [
                        "仓库名称",
                        "统计期间",
                        "月份",
                        "月末日期",
                        "物料名称",
                        "品类",
                    ],
                    as_index=False,
                )["月末库存量（吨）"].sum()
                all_monthly.extend(grouped.to_dict(orient="records"))
                monthly_totals.append(
                    float(grouped["月末库存量（吨）"].sum())
                )

            wh_summary = _summarize_status(details)
            issued = details[details["状态"] == "已出库"]
            current_weighted = float(
                current["加权存放量（吨·天）"].sum()
            )
            current_fee = float(current["仓储费（元）"].sum())
            issued_weighted = float(
                issued["加权存放量（吨·天）"].sum()
            )
            issued_fee = float(issued["仓储费（元）"].sum())
            current_average_rate = (
                current_fee / current_weighted
                if current_weighted
                else float(current["单价（元/吨/天）"].mean())
            )
            issued_average_rate = (
                issued_fee / issued_weighted
                if issued_weighted
                else float(issued["单价（元/吨/天）"].mean())
            )
            all_warehouse.append(
                {
                    "仓库名称": warehouse,
                    "统计期间": period.name,
                    "统计开始日": period.start,
                    "统计截止日": period.end,
                    "原始入库批次数": len(lots),
                    "当前库存批次数": int(
                        (details["状态"] == "仍在库").sum()
                    ),
                    **wh_summary,
                    "期间平均库存量（按月移动平均，吨）": (
                        sum(monthly_totals) / len(monthly_totals)
                        if monthly_totals
                        else 0.0
                    ),
                    "已出库平均单价（元/吨/天）": issued_average_rate,
                    "仍在库平均单价（元/吨/天）": current_average_rate,
                    "平均单价（元/吨/天）": current_average_rate,
                    "月末数据点数": len(month_points),
                    "异常记录数": len(anomalies),
                }
            )

    return EngineResult(
        batch_details=(
            pd.concat(all_details, ignore_index=True)
            if all_details
            else pd.DataFrame()
        ),
        material_summary=pd.DataFrame(all_material),
        category_summary=pd.DataFrame(all_category),
        warehouse_total=pd.DataFrame(all_warehouse),
        monthly_snapshot=pd.DataFrame(all_monthly),
        conservation=pd.DataFrame(all_conservation),
        anomalies=pd.DataFrame(all_anomalies),
    )
