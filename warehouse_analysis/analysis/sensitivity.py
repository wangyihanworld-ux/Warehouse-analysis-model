from __future__ import annotations

from decimal import Decimal

import pandas as pd


SIMULATION_NOTE = "单因素线性模拟，其他因素保持不变"

SENSITIVITY_COLUMNS = [
    "仓库名称",
    "统计期间",
    "统计开始日期",
    "统计截止日期",
    "模拟说明",
    "因素",
    "变化比例",
    "基准值",
    "模拟值",
    "单位",
    "基准仓储费（元）",
    "模拟仓储费（元）",
    "费用变化金额（元）",
    "费用变化比例",
]

RANKING_COLUMNS = [
    "仓库名称",
    "统计期间",
    "统计开始日期",
    "统计截止日期",
    "因素",
    "最大正向影响金额（元）",
    "最大负向影响金额（元）",
    "影响排名",
]

FACTOR_CONFIG = (
    (
        "库存量",
        "平均库存量（吨）",
        "吨",
        (Decimal("-0.10"), Decimal("-0.05"), Decimal("0"), Decimal("0.05"), Decimal("0.10")),
    ),
    (
        "存放天数",
        "平均存放天数",
        "天",
        (Decimal("-0.10"), Decimal("-0.05"), Decimal("0"), Decimal("0.05"), Decimal("0.10")),
    ),
    (
        "费率",
        "平均仓储费率（元/吨/天）",
        "元/吨/天",
        (Decimal("-0.20"), Decimal("-0.10"), Decimal("0"), Decimal("0.10"), Decimal("0.20")),
    ),
)


def _decimal(value: object) -> Decimal:
    """将经营分析数值转换为Decimal，仅用于情景乘法精度控制。"""
    return Decimal(str(value))


def build_fee_sensitivity(
    warehouse_summary: pd.DataFrame,
) -> pd.DataFrame:
    """生成仓储费单因素敏感性分析。

    输入：
    第四阶段 `build_warehouse_summary()` 生成的warehouse_summary。

    输出：
    每个仓库、统计期间、因素和变化比例对应一行的fee_sensitivity
    DataFrame，包含基准值、模拟值、模拟费用及费用变化。

    业务含义：
    回答平均库存量、平均存放天数或平均费率单独变化时，仓储费按线性
    假设发生的变化。

    FIFO影响：
    不调用FIFO、不读取批次、不重新匹配费率。函数只使用经营分析的
    已有指标进行管理情景模拟，不改变任何核心计算结果。
    """
    required = {
        "仓库名称",
        "统计期间",
        "统计开始日期",
        "统计截止日期",
        "平均库存量（吨）",
        "平均存放天数",
        "平均仓储费率（元/吨/天）",
        "仓储费总额（元）",
    }
    missing = [column for column in required if column not in warehouse_summary.columns]
    if missing:
        raise ValueError(
            f"warehouse_summary缺少字段: {', '.join(sorted(missing))}"
        )

    rows: list[dict[str, object]] = []
    for _, summary in warehouse_summary.iterrows():
        base_fee = _decimal(summary["仓储费总额（元）"])
        for factor, value_column, unit, scenarios in FACTOR_CONFIG:
            base_value = _decimal(summary[value_column])
            for change in scenarios:
                multiplier = Decimal("1") + change
                simulated_value = base_value * multiplier
                simulated_fee = base_fee * multiplier
                fee_change = simulated_fee - base_fee
                fee_change_ratio = (
                    fee_change / base_fee if base_fee else Decimal("0")
                )
                rows.append(
                    {
                        "仓库名称": summary["仓库名称"],
                        "统计期间": summary["统计期间"],
                        "统计开始日期": summary["统计开始日期"],
                        "统计截止日期": summary["统计截止日期"],
                        "模拟说明": SIMULATION_NOTE,
                        "因素": factor,
                        "变化比例": float(change),
                        "基准值": float(base_value),
                        "模拟值": float(simulated_value),
                        "单位": unit,
                        "基准仓储费（元）": float(base_fee),
                        "模拟仓储费（元）": float(simulated_fee),
                        "费用变化金额（元）": float(fee_change),
                        "费用变化比例": float(fee_change_ratio),
                    }
                )
    return pd.DataFrame(rows, columns=SENSITIVITY_COLUMNS)


def build_sensitivity_ranking(
    fee_sensitivity: pd.DataFrame,
) -> pd.DataFrame:
    """按因素最大绝对费用影响生成敏感性排名。

    输入：`build_fee_sensitivity()`生成的情景明细。
    输出：每个仓库和统计期间下，各因素的最大正向、最大负向影响及排名。
    业务含义：帮助管理人员识别设定情景范围内最敏感的费用驱动因素。
    FIFO影响：仅汇总模拟结果，不调用或改变FIFO。
    """
    group_columns = [
        "仓库名称",
        "统计期间",
        "统计开始日期",
        "统计截止日期",
        "因素",
    ]
    if fee_sensitivity.empty:
        return pd.DataFrame(columns=RANKING_COLUMNS)
    grouped = (
        fee_sensitivity.groupby(group_columns, as_index=False)[
            "费用变化金额（元）"
        ]
        .agg(
            **{
                "最大正向影响金额（元）": "max",
                "最大负向影响金额（元）": "min",
            }
        )
    )
    grouped["_影响绝对值"] = grouped[
        ["最大正向影响金额（元）", "最大负向影响金额（元）"]
    ].abs().max(axis=1)
    period_columns = [
        "仓库名称",
        "统计期间",
        "统计开始日期",
        "统计截止日期",
    ]
    grouped["影响排名"] = (
        grouped.groupby(period_columns)["_影响绝对值"]
        .rank(method="dense", ascending=False)
        .astype(int)
    )
    grouped = grouped.sort_values(
        period_columns + ["影响排名", "因素"], kind="stable"
    )
    return grouped[RANKING_COLUMNS].reset_index(drop=True)
