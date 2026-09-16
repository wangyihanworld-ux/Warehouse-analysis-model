from __future__ import annotations

from typing import Mapping

import pandas as pd


FIELD_LABELS = {
    "warehouse": "仓库",
    "material": "物料",
    "material_name": "物料名称",
    "quantity": "数量",
    "date": "日期",
    "inventory_date": "日期",
    "direction": "方向",
    "仓库名称": "仓库",
    "物料编码": "物料",
    "物料名称": "物料名称",
    "期初数量": "数量",
    "数量": "数量",
    "日期": "日期",
    "期初批次入库基准日期": "日期",
}


def _mapping_text(mapping: Mapping[str, object]) -> str:
    recognized: dict[str, str] = {}
    for field, column in mapping.items():
        label = FIELD_LABELS.get(str(field))
        if label and column:
            recognized[label] = str(column)
    return "；".join(
        f"{label}→{recognized[label]}"
        for label in ("仓库", "物料", "物料名称", "数量", "日期", "方向")
        if label in recognized
    )


def build_input_check_report(
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
) -> pd.DataFrame:
    """生成输入适配检查结果，只读取标准化DataFrame及其追溯属性。"""
    transaction_mapping = transactions.attrs.get(
        "input_field_mapping",
        transactions.attrs.get("erp_mapping", {}),
    )
    opening_mapping = openings.attrs.get(
        "input_field_mapping",
        openings.attrs.get("erp_mapping", {}),
    )
    quantity_info = transactions.attrs.get("quantity_normalization", {})
    opening_check = openings.attrs.get("opening_inventory_check", {})
    rows = [
        {
            "输入类型": "出入库明细",
            "文件名称": str(transactions.attrs.get("source_file", "")),
            "Sheet名称": str(transactions.attrs.get("source_sheet", "")),
            "识别字段": _mapping_text(transaction_mapping),
            "数量模式": str(
                quantity_info.get("mode", "数量正数+方向模式")
            ),
            "期初日期类型": "",
            "缺失字段": "",
            "建议": "字段识别完成，可进入标准化校验。",
        },
        {
            "输入类型": "期初库存",
            "文件名称": str(openings.attrs.get("source_file", "")),
            "Sheet名称": str(openings.attrs.get("source_sheet", "")),
            "识别字段": _mapping_text(opening_mapping),
            "数量模式": str(opening_check.get("数量模式", "正常模式")),
            "期初日期类型": str(
                openings.attrs.get("opening_date_type", "REAL")
            ),
            "缺失字段": "",
            "建议": (
                "未提供真实日期，采用统计开始日作为FIFO基准日期。"
                if openings.attrs.get("opening_date_type") == "SNAPSHOT"
                else "使用文件中的真实日期。"
            ),
        },
    ]
    return pd.DataFrame(rows)

