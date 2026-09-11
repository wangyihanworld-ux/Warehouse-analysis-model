from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd


OPENING_QUANTITY_ALIASES = (
    "期初数量",
    "期初库存",
    "期初库存数量",
    "初始库存",
    "初始数量",
    "库存数量",
    "可用数量",
    "现存量",
    "数量",
)
NORMAL_MODE = "正常模式"
SIGNED_MODE = "ERP符号模式"


class OpeningInventoryQuantityError(ValueError):
    """期初库存数量检查失败，携带检查统计和负库存明细。"""

    def __init__(
        self,
        *,
        file_path: str,
        sheet_name: str,
        check: dict[str, object],
        negative_records: list[dict[str, object]],
    ) -> None:
        self.file_path = file_path
        self.sheet_name = sheet_name
        self.check = check
        self.negative_records = negative_records
        first = negative_records[0]
        super().__init__(
            "期初库存检查失败\n"
            "原因：发现负库存数据。\n"
            f"文件：{file_path or '未提供'}\n"
            f"Sheet：{sheet_name or '工作表'}\n"
            f"异常数量：{len(negative_records)}条\n"
            f"示例行号：{first['行号']}\n"
            f"仓库：{first['仓库'] or '空'}\n"
            f"物料：{first['物料名称'] or first['物料编码'] or '空'}\n"
            f"数量：{first['数量']}吨\n"
            "建议：请检查ERP期初库存数据。"
        )


class OpeningFieldAmbiguityError(ValueError):
    """期初字段存在多个候选，要求业务人员在GUI中明确选择。"""

    def __init__(
        self,
        *,
        file_path: str,
        sheet_name: str,
        field_name: str,
        candidates: list[str],
    ) -> None:
        self.file_path = file_path
        self.sheet_name = sheet_name
        self.field_name = field_name
        self.candidates = tuple(candidates)
        super().__init__(
            "期初库存字段需要确认\n"
            f"文件：{file_path or '未提供'}\n"
            f"Sheet：{sheet_name or '工作表'}\n"
            f"字段：{field_name}\n"
            f"候选字段：{', '.join(candidates)}\n"
            "建议：请在字段确认窗口中明确选择一个字段。"
        )


def find_opening_quantity_candidates(columns: list[object]) -> list[str]:
    """按业务优先级返回期初数量候选列，不进行猜测。"""
    actual = {str(column).strip(): str(column) for column in columns}
    return [
        actual[alias]
        for alias in OPENING_QUANTITY_ALIASES
        if alias in actual
    ]


def _quantity_column(
    frame: pd.DataFrame,
    *,
    file_path: str,
    sheet_name: str,
    selected_column: str | None = None,
) -> str:
    candidates = find_opening_quantity_candidates(list(frame.columns))
    if selected_column:
        if selected_column not in candidates:
            raise ValueError(f"选择的期初数量字段不存在：{selected_column}")
        return selected_column
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise OpeningFieldAmbiguityError(
            file_path=file_path,
            sheet_name=sheet_name,
            field_name="期初数量",
            candidates=candidates,
        )
    raise ValueError(
        "期初库存检查失败\n原因：未找到数量字段。\n"
        f"支持字段：{', '.join(OPENING_QUANTITY_ALIASES)}"
    )


def _decimal(
    value: Any,
    *,
    file_path: str,
    sheet_name: str,
    row_number: int,
) -> Decimal:
    if pd.isna(value) or str(value).replace("\u00a0", " ").strip() == "":
        raise ValueError(
            "期初库存检查失败\n"
            f"文件：{file_path or '未提供'}\n"
            f"Sheet：{sheet_name or '工作表'}\n"
            f"行号：{row_number}\n原因：期初数量为空。\n"
            "建议：请补充期初数量后重新导入。"
        )
    text = str(value).replace("\u00a0", " ").replace(",", "").strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            "期初库存检查失败\n"
            f"文件：{file_path or '未提供'}\n"
            f"Sheet：{sheet_name or '工作表'}\n"
            f"行号：{row_number}\n原因：期初数量无法转换为数字（{value}）。\n"
            "建议：请确认数量字段仅包含有效数字。"
        ) from exc
    if not number.is_finite():
        raise ValueError(
            "期初库存检查失败\n"
            f"文件：{file_path or '未提供'}\n"
            f"Sheet：{sheet_name or '工作表'}\n"
            f"行号：{row_number}\n原因：期初数量不是有限数字（{value}）。"
        )
    return number


def _text(row: pd.Series, *columns: str) -> str:
    for column in columns:
        if column in row.index and not pd.isna(row[column]):
            value = str(row[column]).replace("\u00a0", " ").strip()
            if value:
                return value
    return ""


def _is_summary_row(row: pd.Series) -> bool:
    warehouse = _text(row, "仓库名称", "仓库", "库存组织名称", "子库名称")
    material = _text(
        row,
        "物料名称",
        "物料描述",
        "物料编码",
        "EBS物料编码",
        "物料号",
    )
    summary_tokens = {"合计", "总计", "汇总", "小计"}
    return (
        not warehouse
        or not material
        or material.replace("：", "").replace(":", "").strip() in summary_tokens
    )


def normalize_opening_inventory_quantity(
    frame: pd.DataFrame,
    *,
    file_path: str | Path | None = None,
    sheet_name: str | None = None,
    quantity_column: str | None = None,
) -> pd.DataFrame:
    """标准化期初库存数量并生成检查统计。

    输入：包含任一支持数量字段的原始或标准期初库存DataFrame。
    输出：数量列统一为正Decimal、零数量和汇总行已过滤的副本。
    业务含义：负库存不取绝对值，生成可追溯明细并停止计算。
    FIFO影响：仅在进入FIFO前校验期初数据，不修改FIFO算法或计算口径。
    """
    if "opening_inventory_check" in frame.attrs:
        return frame.copy()

    source_file = str(
        file_path
        or frame.attrs.get("source_file")
        or ""
    )
    source_sheet = str(
        sheet_name
        or frame.attrs.get("source_sheet")
        or "工作表"
    )
    quantity_column = _quantity_column(
        frame,
        file_path=source_file,
        sheet_name=source_sheet,
        selected_column=quantity_column,
    )
    result = frame.copy()
    result[quantity_column] = result[quantity_column].astype(object)
    total = len(result)
    filtered_indexes: list[object] = []
    zero_indexes: list[object] = []
    valid_indexes: list[object] = []
    negative_records: list[dict[str, object]] = []

    for position, (index, row) in enumerate(result.iterrows(), start=2):
        if _is_summary_row(row):
            filtered_indexes.append(index)
            continue
        quantity = _decimal(
            row[quantity_column],
            file_path=source_file,
            sheet_name=source_sheet,
            row_number=position,
        )
        result.at[index, quantity_column] = quantity
        if quantity < 0:
            negative_records.append(
                {
                    "文件": source_file,
                    "Sheet": source_sheet,
                    "行号": position,
                    "仓库": _text(
                        row,
                        "仓库名称",
                        "仓库",
                        "库存组织名称",
                        "子库名称",
                    ),
                    "物料编码": _text(
                        row, "物料编码", "EBS物料编码", "物料号"
                    ),
                    "物料名称": _text(row, "物料名称", "物料描述"),
                    "数量": quantity,
                }
            )
        elif quantity == 0:
            zero_indexes.append(index)
        else:
            valid_indexes.append(index)

    mode = SIGNED_MODE if negative_records else NORMAL_MODE
    check: dict[str, object] = {
        "数量模式": mode,
        "总记录数": total,
        "有效库存记录": len(valid_indexes),
        "过滤汇总记录": len(filtered_indexes),
        "零数量记录": len(zero_indexes),
        "负库存记录": len(negative_records),
        "最终进入FIFO": len(valid_indexes),
    }
    if negative_records:
        raise OpeningInventoryQuantityError(
            file_path=source_file,
            sheet_name=source_sheet,
            check=check,
            negative_records=negative_records,
        )

    result = result.loc[valid_indexes].copy()
    if quantity_column != "期初数量":
        result = result.rename(columns={quantity_column: "期初数量"})
    result.attrs.update(frame.attrs)
    result.attrs["source_file"] = source_file
    result.attrs["source_sheet"] = source_sheet
    result.attrs["opening_inventory_check"] = check
    result.attrs["opening_inventory_negative_records"] = []
    return result
