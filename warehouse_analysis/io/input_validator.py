from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd


@dataclass(frozen=True)
class InputValidationIssue:
    problem: str
    location: str
    finding: str
    suggestion: str

    def format(self) -> str:
        return (
            f"问题：\n{self.problem}\n\n"
            f"位置：\n{self.location}\n\n"
            f"发现：\n{self.finding}\n\n"
            f"建议：\n{self.suggestion}"
        )


class InputValidationError(ValueError):
    """FIFO运行前发现的业务输入问题。"""

    def __init__(self, issues: list[InputValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n\n".join(issue.format() for issue in issues))


def validate_input_files(
    transaction_file: str | Path,
    opening_file: str | Path | None,
) -> None:
    """检查业务输入文件是否存在，不读取或修改文件。"""
    issues: list[InputValidationIssue] = []
    for label, value in (("出入库明细文件", transaction_file), ("期初库存文件", opening_file)):
        if value is None:
            continue
        path = Path(value)
        if not path.is_file():
            issues.append(InputValidationIssue(
                "文件不存在", f"{label} / {path}", "未找到指定文件",
                "重新选择存在且有读取权限的ERP文件。",
            ))
    if issues:
        raise InputValidationError(issues)


def _source_location(frame: pd.DataFrame, default: str) -> str:
    source = str(frame.attrs.get("source_file", "")).strip() or default
    sheet = str(frame.attrs.get("source_sheet", "")).strip()
    return f"{source} / Sheet{sheet}" if sheet else source


def _mapping(frame: pd.DataFrame) -> Mapping[str, object]:
    return frame.attrs.get("input_field_mapping", frame.attrs.get("erp_mapping", {}))


def validate_standardized_inputs(
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
) -> None:
    """校验标准化结果；成功后才允许进入FIFO核心。"""
    issues: list[InputValidationIssue] = []
    tx_location = _source_location(transactions, "出入库明细")
    op_location = _source_location(openings, "期初库存")

    required_tx = ("仓库名称", "物料名称", "数量", "日期")
    required_op = ("仓库名称", "物料名称", "期初数量", "期初批次入库基准日期")
    for frame, required, location in (
        (transactions, required_tx, tx_location),
        (openings, required_op, op_location),
    ):
        missing = [column for column in required if column not in frame.columns]
        if missing:
            issues.append(InputValidationIssue(
                f"缺少{'、'.join(missing)}字段", location,
                f"字段映射后未生成：{'、'.join(missing)}",
                "检查ERP字段映射配置，或在字段确认窗口选择正确列。",
            ))

    if openings.empty:
        issues.append(InputValidationIssue(
            "期初库存为空", op_location, "没有可进入FIFO的有效期初库存记录",
            "检查仓库筛选、数量字段、零库存过滤及期初快照期间。",
        ))

    for frame, location in ((transactions, tx_location), (openings, op_location)):
        warehouse_column = "仓库名称"
        material_column = "物料名称"
        if warehouse_column in frame:
            blank = frame[warehouse_column].isna() | frame[warehouse_column].astype(str).str.strip().eq("")
            if blank.any():
                issues.append(InputValidationIssue(
                    "仓库无法识别", location, f"发现{int(blank.sum())}条仓库为空的记录",
                    "补充仓库名称，或修正warehouse字段映射。",
                ))
        if material_column in frame:
            blank = frame[material_column].isna() | frame[material_column].astype(str).str.strip().eq("")
            if blank.any():
                issues.append(InputValidationIssue(
                    "物料无法识别", location, f"发现{int(blank.sum())}条物料为空的记录",
                    "补充物料编码/名称，或修正material字段映射。",
                ))

    for frame, column, location in (
        (transactions, "数量", tx_location),
        (openings, "期初数量", op_location),
    ):
        if column in frame:
            numeric = pd.to_numeric(frame[column], errors="coerce")
            invalid = numeric.isna()
            if invalid.any():
                issues.append(InputValidationIssue(
                    "数量不合法", location, f"{column}存在{int(invalid.sum())}条空值或非数字",
                    "填写有效数字；期初库存不得为负数，流水数量不得为零。",
                ))

    for frame, column, location in (
        (transactions, "日期", tx_location),
        (openings, "期初批次入库基准日期", op_location),
    ):
        if column in frame:
            parsed = pd.to_datetime(frame[column], errors="coerce")
            invalid = parsed.isna()
            if invalid.any():
                issues.append(InputValidationIssue(
                    "日期不合法", location, f"{column}存在{int(invalid.sum())}条空值或无效日期",
                    "使用YYYY-MM-DD格式，或确认库存快照采用统计开始日。",
                ))

    for frame, label, location in ((transactions, "流水", tx_location), (openings, "期初", op_location)):
        if frame.attrs.get("source_sheet", None) == "":
            issues.append(InputValidationIssue(
                "Sheet未识别", location, f"{label}数据缺少来源Sheet信息",
                "确认ERP工作簿包含可识别字段的明细Sheet。",
            ))
        mapping = _mapping(frame)
        if frame.attrs.get("erp_detection") and not mapping:
            issues.append(InputValidationIssue(
                "字段映射未记录", location, "未取得ERP字段映射结果",
                "重新执行字段识别，并确认仓库、物料、数量和日期字段。",
            ))

    if issues:
        raise InputValidationError(issues)
