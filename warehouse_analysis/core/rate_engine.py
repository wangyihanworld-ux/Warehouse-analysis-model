from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator

import pandas as pd


@dataclass(frozen=True)
class RateMatch:
    """一次费率匹配结果。

    输入：由RateResolver从配置表选中的规则。
    输出：单价、说明、匹配层级、对象、生效日及配置源行号。
    业务含义：在不改变计费结果的前提下增加规则追溯信息。
    FIFO影响：不参与FIFO队列和批次扣减，不改变FIFO结果。

    为保持旧调用兼容，迭代时只返回单价和计费说明。
    """

    rate: Decimal
    note: str
    level: str
    target: str
    effective_date: pd.Timestamp
    source_row: int

    def __iter__(self) -> Iterator[Decimal | str]:
        yield self.rate
        yield self.note


class RateResolver:
    """按物料、品类、仓库全部的优先级解析费率。

    输入：标准化费率表、物料集合和品类集合。
    输出：resolve返回RateMatch，可继续按旧接口解包为单价和说明。
    业务含义：应用统计日期之前最近生效的最高优先级规则。
    FIFO影响：只影响仓储费单价，不改变FIFO顺序、数量和拆分结果。
    """

    def __init__(
        self, rates: pd.DataFrame, materials: set[str], categories: set[str]
    ):
        self.rates = rates.copy()
        inferred = []
        for _, row in self.rates.iterrows():
            level = str(row["适用层级"]).strip()
            target = str(row["品类或物料"]).strip()
            if level in {"物料", "品类", "全部"}:
                inferred.append(level)
            elif target in {"全部", "*"}:
                inferred.append("全部")
            elif target in materials:
                inferred.append("物料")
            elif target in categories:
                inferred.append("品类")
            else:
                raise ValueError(
                    f"计价规则无法判断适用层级，仓库={row['仓库名称']}，对象={target}；"
                    "请填写适用层级为物料、品类或全部"
                )
        self.rates["_层级"] = inferred

    def resolve(
        self,
        warehouse: str,
        material: str,
        category: str,
        effective_date: pd.Timestamp,
    ) -> RateMatch:
        """匹配指定仓库、物料、品类和日期的费率，不改变FIFO结果。"""
        candidates = self.rates[
            (self.rates["仓库名称"] == warehouse)
            & (self.rates["生效日期"] <= effective_date)
        ].copy()
        for level, target in (
            ("物料", material),
            ("品类", category),
            ("全部", None),
        ):
            level_rows = candidates[candidates["_层级"] == level]
            if level != "全部":
                level_rows = level_rows[level_rows["品类或物料"] == target]
            if not level_rows.empty:
                selected = level_rows.sort_values(
                    ["生效日期", "源数据行号"], kind="stable"
                ).iloc[-1]
                selected_target = (
                    str(selected["品类或物料"]) if level != "全部" else "全部"
                )
                return RateMatch(
                    rate=selected["单价"],
                    note=str(selected["计费方式说明"]),
                    level=level,
                    target=selected_target,
                    effective_date=pd.Timestamp(selected["生效日期"]),
                    source_row=int(selected["源数据行号"]),
                )
        raise ValueError(
            f"没有匹配的计价规则: 仓库={warehouse}, 物料={material}, "
            f"品类={category}, 日期={effective_date.date()}"
        )
