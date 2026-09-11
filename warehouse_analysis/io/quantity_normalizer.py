from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd


SIGNED_MODE = "ERP数量带符号模式"
DIRECTION_MODE = "数量正数+方向模式"


class QuantityNormalizationError(ValueError):
    """数量符号与方向冲突或数量无效时的可追溯输入错误。"""


def _decimal(value: Any, sheet_name: str, row_number: int) -> Decimal:
    if pd.isna(value) or str(value).strip() == "":
        raise QuantityNormalizationError(
            f"Sheet：{sheet_name}\n行号：{row_number}\n问题：数量为空\n"
            "建议：请补充数量后重新导入。"
        )
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise QuantityNormalizationError(
            f"Sheet：{sheet_name}\n行号：{row_number}\n"
            f"问题：数量无法转换为数字（{value}）\n"
            "建议：请确认ERP数量字段仅包含有效数字。"
        ) from exc
    if not number.is_finite():
        raise QuantityNormalizationError(
            f"Sheet：{sheet_name}\n行号：{row_number}\n"
            f"问题：数量不是有限数字（{value}）\n"
            "建议：请修正数量后重新导入。"
        )
    return number


def _direction(value: Any, sheet_name: str, row_number: int) -> str:
    text = "" if pd.isna(value) else str(value).strip()
    aliases = {"入库": "入库", "收货": "入库", "出库": "出库", "发货": "出库"}
    if text not in aliases:
        raise QuantityNormalizationError(
            f"Sheet：{sheet_name}\n行号：{row_number}\n"
            f"问题：方向字段无法识别（{text or '空'}）\n"
            "建议：方向请填写入库/出库，或使用系统支持的收货/发货别名。"
        )
    return aliases[text]


def normalize_transaction_quantity(df: pd.DataFrame) -> pd.DataFrame:
    """把流水数量统一为FIFO要求的正数，并保留明确的入库/出库方向。

    输入：至少包含“数量”和“方向”的标准流水DataFrame；可通过
    ``df.attrs["source_sheet"]`` 提供源Sheet名称。
    输出：数量为正数的流水副本；零数量行不进入结果，并记录在
    ``result.attrs["quantity_anomalies"]``。
    业务含义：兼容ERP正入负出与历史正数+方向两种输入。
    FIFO影响：仅在FIFO之前标准化输入，不修改FIFO顺序、拆分或计算逻辑。
    """
    missing = [column for column in ("数量", "方向") if column not in df.columns]
    if missing:
        raise QuantityNormalizationError(
            f"缺少字段：{', '.join(missing)}\n建议：请补充字段后重新导入。"
        )
    if "quantity_normalization" in df.attrs:
        return df.copy()

    source_sheet = str(df.attrs.get("source_sheet") or "工作表")
    parsed: list[tuple[object, int, Decimal, str]] = []
    for position, (index, row) in enumerate(df.iterrows(), start=2):
        quantity = _decimal(row["数量"], source_sheet, position)
        direction = _direction(row["方向"], source_sheet, position)
        parsed.append((index, position, quantity, direction))

    mode = SIGNED_MODE if any(item[2] < 0 for item in parsed) else DIRECTION_MODE
    normalized = df.copy()
    normalized["数量"] = normalized["数量"].astype(object)
    zero_anomalies: list[dict[str, object]] = []
    inbound_total = Decimal("0")
    outbound_total = Decimal("0")
    keep_indexes: list[object] = []

    for index, row_number, quantity, direction in parsed:
        if quantity == 0:
            zero_anomalies.append(
                {
                    "仓库名称": (
                        normalized.at[index, "仓库名称"]
                        if "仓库名称" in normalized.columns
                        else ""
                    ),
                    "物料名称": (
                        normalized.at[index, "物料名称"]
                        if "物料名称" in normalized.columns
                        else ""
                    ),
                    "方向": direction,
                    "源数据行号": row_number,
                    "异常原因": "输入异常：数量为0；该记录未进入FIFO",
                    "源Sheet": source_sheet,
                }
            )
            continue

        if mode == SIGNED_MODE:
            sign_direction = "入库" if quantity > 0 else "出库"
            if direction != sign_direction:
                raise QuantityNormalizationError(
                    f"Sheet：{source_sheet}\n行号：{row_number}\n"
                    f"问题：数量符号与方向字段冲突（方向={direction}，数量={quantity}）\n"
                    "建议：请确认ERP字段定义；正数应为入库，负数应为出库。"
                )
            direction = sign_direction

        absolute_quantity = abs(quantity)
        normalized.at[index, "方向"] = direction
        normalized.at[index, "数量"] = absolute_quantity
        keep_indexes.append(index)
        if direction == "入库":
            inbound_total += absolute_quantity
        else:
            outbound_total += absolute_quantity

    normalized = normalized.loc[keep_indexes].copy()
    normalized.attrs.update(df.attrs)
    normalized.attrs["quantity_normalization"] = {
        "mode": mode,
        "inbound_total": inbound_total,
        "outbound_total": outbound_total,
        "zero_count": len(zero_anomalies),
    }
    normalized.attrs["quantity_anomalies"] = zero_anomalies
    return normalized
