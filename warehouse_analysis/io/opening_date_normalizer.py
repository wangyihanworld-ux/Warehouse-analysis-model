from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd


REAL = "REAL"
SNAPSHOT = "SNAPSHOT"


def _decimal_or_zero(value: Any) -> Decimal:
    if pd.isna(value):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def normalize_opening_inventory_dates(
    openings: pd.DataFrame,
    start_date: object,
) -> pd.DataFrame:
    """统一期初库存日期属性，不改变FIFO算法。

    输入：期初库存DataFrame和明确的统计开始日期。
    输出：保留原始库存日期；SNAPSHOT批次的FIFO基准日期改为统计开始日。
    业务含义：快照日期只代表库存时点，不被误认为真实批次入库日期。
    FIFO影响：仅修正进入FIFO前的输入日期；REAL日期保持不变。
    """
    result = openings.copy()
    result.attrs.update(openings.attrs)
    start = pd.Timestamp(start_date).normalize()

    if "opening_date_type" not in result.columns:
        default_type = str(
            result.attrs.get("opening_date_type", REAL)
        ).strip().upper()
        result["opening_date_type"] = default_type
    result["opening_date_type"] = (
        result["opening_date_type"].fillna(REAL).astype(str).str.strip().str.upper()
    )
    invalid = sorted(set(result["opening_date_type"]) - {REAL, SNAPSHOT})
    if invalid:
        raise ValueError(
            f"期初库存存在不支持的日期类型：{invalid}；仅支持REAL或SNAPSHOT"
        )

    if "original_inventory_date" not in result.columns:
        result["original_inventory_date"] = result[
            "期初批次入库基准日期"
        ]
    result["original_inventory_date"] = pd.to_datetime(
        result["original_inventory_date"], errors="coerce"
    )
    result["期初批次入库基准日期"] = pd.to_datetime(
        result["期初批次入库基准日期"], errors="coerce"
    )
    missing_original = result["original_inventory_date"].isna()
    if missing_original.any():
        result.loc[missing_original, "original_inventory_date"] = pd.to_datetime(
            result.loc[missing_original, "期初批次入库基准日期"],
            errors="coerce",
        )

    snapshot_mask = result["opening_date_type"] == SNAPSHOT
    result.loc[snapshot_mask, "期初批次入库基准日期"] = start
    result.attrs["opening_date_type"] = (
        SNAPSHOT if snapshot_mask.any() else REAL
    )
    result.attrs["opening_baseline_date"] = start
    result.attrs["opening_inventory_source"] = str(
        openings.attrs.get("opening_inventory_source", "")
    )
    result.attrs["opening_date_description"] = (
        "期初库存采用统计开始日作为FIFO起始日期"
        if snapshot_mask.any()
        else "期初库存采用真实入库日期"
    )
    return result


def filter_transactions_before_start(
    transactions: pd.DataFrame,
    start_date: object,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """排除统计开始日前流水并返回可审计统计。

    输入：标准流水DataFrame和统计开始日期。
    输出：保留原DataFrame副本、期间内流水以及历史流水统计。
    业务含义：期初快照已经代表开始时点库存，历史流水不能再次进入FIFO。
    FIFO影响：仅限定本次计算输入范围，不删除或修改源文件数据。
    """
    source = transactions.copy()
    source.attrs.update(transactions.attrs)
    start = pd.Timestamp(start_date).normalize()
    dates = pd.to_datetime(source["日期"], errors="coerce")
    historical_mask = dates < start
    historical = source.loc[historical_mask].copy()
    filtered = source.loc[~historical_mask].copy()
    filtered.attrs.update(source.attrs)

    quantity_total = sum(
        (_decimal_or_zero(value) for value in historical.get("数量", [])),
        Decimal("0"),
    )
    amount_column = next(
        (
            column
            for column in ("金额", "仓储费", "交易金额", "含税金额")
            if column in historical.columns
        ),
        None,
    )
    amount_total = (
        sum(
            (
                _decimal_or_zero(value)
                for value in historical[amount_column]
            ),
            Decimal("0"),
        )
        if amount_column
        else None
    )
    check: dict[str, object] = {
        "统计开始日期": start,
        "历史流水记录数": int(historical_mask.sum()),
        "历史流水数量": quantity_total,
        "历史流水金额": amount_total,
        "金额字段": amount_column or "",
        "原始流水记录数": len(source),
        "进入FIFO流水记录数": len(filtered),
    }
    filtered.attrs["historical_transaction_check"] = check
    filtered.attrs["historical_transactions"] = historical
    return filtered, check


def snapshot_source_label(path: str | Path | None) -> str:
    """返回Oracle库存快照的统一业务来源说明。"""
    if path and str(path).lower().endswith(".xls"):
        return "Oracle BI Publisher HTML库存快照"
    return "库存快照"
