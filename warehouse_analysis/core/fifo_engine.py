from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal
from typing import Any

import pandas as pd


def _rebuild_fifo(
    warehouse: str,
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
    end_date: pd.Timestamp,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """按截止时点重建指定仓库的FIFO库存。

    输入：仓库名称、已标准化流水、已标准化期初库存和统计截止日。
    输出：原始入库批次、出库拆分记录和库存不足异常三组字典列表。
    业务含义：期初及入库进入队尾，出库从最早批次依次扣减。
    FIFO影响：这是旧引擎核心算法的等价迁移，不改变顺序、拆分、异常
    策略或Decimal精度。
    """
    queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    lots: list[dict[str, Any]] = []
    allocations: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    def add_lot(
        *,
        material: str,
        category: str,
        inbound_date: pd.Timestamp,
        quantity: Decimal,
        batch_no: str,
        source_row: int,
        source_type: str,
    ) -> None:
        engine_id = f"{warehouse}|{material}|{source_type}|{source_row}|{batch_no}"
        lot = {
            "引擎批次ID": engine_id,
            "仓库名称": warehouse,
            "物料名称": material,
            "品类": category,
            "原始批次号": batch_no,
            "批次来源": source_type,
            "源数据行号": int(source_row),
            "入库日期": inbound_date,
            "原始入库数量": quantity,
            "剩余数量": quantity,
        }
        queues[material].append(lot)
        lots.append(lot)

    wh_opening = openings[
        (openings["仓库名称"] == warehouse)
        & (openings["期初批次入库基准日期"] <= end_date)
    ].sort_values(["期初批次入库基准日期", "源数据行号"], kind="stable")
    for _, row in wh_opening.iterrows():
        add_lot(
            material=row["物料名称"],
            category=row["品类"],
            inbound_date=row["期初批次入库基准日期"],
            quantity=row["期初数量"],
            batch_no=str(row["期初批次号"]),
            source_row=int(row["源数据行号"]),
            source_type="期初存货",
        )

    end_exclusive = end_date.normalize() + pd.Timedelta(days=1)
    wh_tx = transactions[
        (transactions["仓库名称"] == warehouse)
        & (transactions["日期"] < end_exclusive)
    ].sort_values(["日期", "源数据行号"], kind="stable")
    for _, row in wh_tx.iterrows():
        material = row["物料名称"]
        category = row["品类"]
        direction = row["方向"]
        quantity = row["数量"]
        queue = queues[material]
        if direction == "入库":
            add_lot(
                material=material,
                category=category,
                inbound_date=row["日期"],
                quantity=quantity,
                batch_no=str(row["批次号"]),
                source_row=int(row["源数据行号"]),
                source_type="流水入库",
            )
            continue

        available = sum(
            (lot["剩余数量"] for lot in queue), Decimal("0")
        )
        if available < quantity:
            anomalies.append(
                {
                    "仓库名称": warehouse,
                    "物料名称": material,
                    "品类": category,
                    "异常日期": row["日期"],
                    "方向": direction,
                    "批次号": row["批次号"],
                    "源数据行号": int(row["源数据行号"]),
                    "出库数量": float(quantity),
                    "当时FIFO队列剩余量": float(available),
                    "差额": float(quantity - available),
                    "异常原因": "出库数量大于当时FIFO队列剩余量；本次出库未分摊",
                }
            )
            continue
        remaining_issue = quantity
        while remaining_issue > 0:
            lot = queue[0]
            allocated = min(lot["剩余数量"], remaining_issue)
            days = (
                row["日期"].normalize() - lot["入库日期"].normalize()
            ).days
            if days < 0:
                raise ValueError(
                    f"出库日期早于入库日期: 仓库={warehouse}, 物料={material}, "
                    f"批次={lot['引擎批次ID']}"
                )
            allocations.append(
                {
                    "引擎批次ID": lot["引擎批次ID"],
                    "出库日期": row["日期"],
                    "出库批次号": row["批次号"],
                    "出库源数据行号": int(row["源数据行号"]),
                    "出库数量": allocated,
                    "存放天数": days,
                }
            )
            lot["剩余数量"] -= allocated
            remaining_issue -= allocated
            if lot["剩余数量"] == 0:
                queue.popleft()
    return lots, allocations, anomalies
