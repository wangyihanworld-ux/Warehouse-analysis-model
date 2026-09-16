from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

import pandas as pd

from warehouse_analysis.io.excel_loader import (
    InputWorkbookError,
    is_oracle_bi_html,
)
from warehouse_analysis.io.source_adapters import _OracleHtmlTableParser
from warehouse_analysis.io.quantity_normalizer import (
    normalize_transaction_quantity,
)
from warehouse_analysis.io.opening_quantity_normalizer import (
    normalize_opening_inventory_quantity,
)
from warehouse_analysis.io.category_normalizer import standardize_category_parts


TRANSACTION_FIELDS = ("date", "warehouse", "material", "quantity", "direction")
OPENING_FIELDS = ("warehouse", "material", "quantity")
STANDARD_TRANSACTION_COLUMNS = [
    "仓库名称",
    "物料名称",
    "品类",
    "方向",
    "日期",
    "数量",
    "批次号",
]
STANDARD_OPENING_COLUMNS = [
    "仓库名称",
    "物料名称",
    "品类",
    "期初数量",
    "期初批次入库基准日期",
    "期初批次号",
]


def default_mapping_path() -> Path:
    """返回源码或EXE发布目录中的ERP字段映射配置。"""
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return root / "config" / "field_mapping.json"


@dataclass(frozen=True)
class ERPDetectionResult:
    """ERP字段检测结果，不包含任何计算逻辑。"""

    file_path: Path
    sheet_name: str
    field_candidates: dict[str, tuple[str, ...]]
    row_count: int
    table_kind: str

    @property
    def resolved_mapping(self) -> dict[str, str]:
        resolved = {
            field: candidates[0]
            for field, candidates in self.field_candidates.items()
            if len(candidates) == 1
        }
        material_candidates = self.field_candidates.get("material", ())
        if "EBS物料编码" in material_candidates:
            resolved["material"] = "EBS物料编码"
        return resolved

    @property
    def ambiguous_fields(self) -> dict[str, tuple[str, ...]]:
        return {
            field: candidates
            for field, candidates in self.field_candidates.items()
            if len(candidates) > 1
            and not (field == "material" and "EBS物料编码" in candidates)
        }


class ERPFieldAmbiguityError(ValueError):
    """存在多个字段候选，必须由业务人员明确选择。"""

    def __init__(self, detection: ERPDetectionResult) -> None:
        self.detection = detection
        details = "；".join(
            f"{field}：{', '.join(candidates)}"
            for field, candidates in detection.ambiguous_fields.items()
        )
        super().__init__(
            "ERP字段存在多个候选，禁止自动选择。\n"
            f"文件：{detection.file_path}\n"
            f"Sheet：{detection.sheet_name}\n"
            f"候选字段：{details}\n"
            "建议：请在字段确认窗口中明确选择。"
        )


def load_field_mapping(path: str | Path | None = None) -> dict[str, list[str]]:
    """读取可维护的ERP字段别名配置。"""
    target = Path(path) if path is not None else default_mapping_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"ERP字段映射配置无法读取：{target}；{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"ERP字段映射配置必须是JSON对象：{target}")
    return {
        str(field): [str(value).strip() for value in values]
        for field, values in payload.items()
        if isinstance(values, list)
    }


def _read_html_tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    parser = _OracleHtmlTableParser()
    parser.feed(path.read_text(encoding="utf-8"))
    results: list[tuple[str, pd.DataFrame]] = []
    for table_number, table in enumerate(parser.tables, start=1):
        for header_index, row in enumerate(table):
            nonempty = [str(value).strip() for value in row if str(value).strip()]
            if len(nonempty) < 2:
                continue
            width = len(row)
            records = [
                data_row[:width]
                for data_row in table[header_index + 1 :]
                if len(data_row) >= width
            ]
            if records:
                results.append(
                    (
                        f"HTML表{table_number}_行{header_index + 1}",
                        pd.DataFrame(records, columns=row),
                    )
                )
    return results


def _read_tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    """读取真正Excel或Oracle HTML伪xls中的所有候选表。"""
    if is_oracle_bi_html(path):
        try:
            tables = _read_html_tables(path)
        except (OSError, UnicodeError) as exc:
            raise InputWorkbookError(
                file_path=path,
                sheet_name="",
                format_name="Oracle BI Publisher HTML报表",
                error="HTML报表无法读取或编码无效",
                suggestion="请重新从Oracle BI Publisher导出期初库存报表。",
            ) from exc
        if tables:
            return tables
        raise InputWorkbookError(
            file_path=path,
            sheet_name="",
            format_name="Oracle BI Publisher HTML报表",
            error="未找到包含期初库存字段的数据表\n缺失：仓库/物料/数量",
            suggestion="确认导出的库存报表包含完整字段。",
        )
    try:
        with pd.ExcelFile(path) as workbook:
            return [
                (sheet, pd.read_excel(path, sheet_name=sheet))
                for sheet in workbook.sheet_names
            ]
    except (ValueError, OSError):
        if path.suffix.lower() == ".xls":
            try:
                tables = _read_html_tables(path)
            except (OSError, UnicodeError) as exc:
                raise InputWorkbookError(
                    file_path=path,
                    sheet_name="工作簿",
                    error=str(exc),
                    suggestion="请提供可读取的ERP Excel或Oracle HTML伪xls文件。",
                ) from exc
            if tables:
                return tables
        raise InputWorkbookError(
            file_path=path,
            sheet_name="工作簿",
            error="文件不是可识别的Excel或Oracle HTML伪xls。",
            suggestion="请重新从ERP导出Excel文件后导入。",
        )


def _candidates(
    columns: list[object],
    aliases: Mapping[str, list[str]],
) -> dict[str, tuple[str, ...]]:
    actual = {str(column).strip(): str(column) for column in columns}
    return {
        field: tuple(actual[alias] for alias in choices if alias in actual)
        for field, choices in aliases.items()
    }


def detect_erp_file(
    path: str | Path,
    *,
    table_kind: str = "transaction",
    mapping_path: str | Path | None = None,
) -> ERPDetectionResult:
    """不依赖文件名或Sheet名，通过字段识别ERP流水或期初库存。"""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    aliases = load_field_mapping(mapping_path)
    required = (
        TRANSACTION_FIELDS if table_kind == "transaction" else OPENING_FIELDS
    )
    matches: list[ERPDetectionResult] = []
    inspected: list[tuple[str, dict[str, tuple[str, ...]]]] = []
    for sheet_name, frame in _read_tables(source):
        candidates = _candidates(list(frame.columns), aliases)
        inspected.append((sheet_name, candidates))
        if all(candidates.get(field) for field in required):
            matches.append(
                ERPDetectionResult(
                    source,
                    sheet_name,
                    candidates,
                    len(frame),
                    table_kind,
                )
            )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sheets = "、".join(result.sheet_name for result in matches)
        raise InputWorkbookError(
            file_path=source,
            sheet_name=sheets,
            error="多个工作表同时满足ERP字段识别条件。",
            suggestion="请保留目标工作表，或将其他候选表另存为单独文件。",
        )
    best_sheet, best = max(
        inspected,
        key=lambda item: sum(bool(item[1].get(field)) for field in required),
        default=("工作簿", {}),
    )
    missing = [field for field in required if not best.get(field)]
    raise InputWorkbookError(
        file_path=source,
        sheet_name=best_sheet,
        error=f"无法识别ERP{('流水' if table_kind == 'transaction' else '期初库存')}，"
        f"缺少字段类型：{', '.join(missing)}",
        suggestion="请在config/field_mapping.json增加实际列名，或补充缺失字段。",
    )


def _selected_mapping(
    detection: ERPDetectionResult,
    overrides: Mapping[str, str] | None,
    required: tuple[str, ...],
) -> dict[str, str]:
    selected = detection.resolved_mapping
    if overrides:
        for field, column in overrides.items():
            if column not in detection.field_candidates.get(field, ()):
                raise ValueError(f"字段{field}不能选择不存在的列：{column}")
            selected[field] = column
    unresolved = [
        field
        for field in required
        if field not in selected
    ]
    if unresolved:
        raise ERPFieldAmbiguityError(detection)
    return selected


def _load_detected_frame(detection: ERPDetectionResult) -> pd.DataFrame:
    tables = dict(_read_tables(detection.file_path))
    return tables[detection.sheet_name].copy()


def _text(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).replace("\u00a0", " ").strip()


def _decimal(value: object, field: str, row_number: int) -> Decimal:
    if pd.isna(value) or _text(value) == "":
        raise ValueError(f"ERP第{row_number}行{field}为空")
    try:
        result = Decimal(_text(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"ERP第{row_number}行{field}不是有效数字：{value}"
        ) from exc
    if not result.is_finite():
        raise ValueError(f"ERP第{row_number}行{field}不是有限数字：{value}")
    return result


def _direction(value: object, row_number: int) -> str:
    text = _text(value)
    inbound_tokens = ("入库", "收货", "入账", "盘盈", "调拨入")
    outbound_tokens = ("出库", "发货", "发出", "盘亏", "调拨出")
    if text in {"入", "I", "IN", "in"} or any(
        token in text for token in inbound_tokens
    ):
        return "入库"
    if text in {"出", "O", "OUT", "out"} or any(
        token in text for token in outbound_tokens
    ):
        return "出库"
    raise ValueError(f"ERP第{row_number}行方向无法识别：{text}")


def _category(row: pd.Series, mapping: Mapping[str, str]) -> str:
    return standardize_category_parts(
        _text(row[mapping[field]]) if mapping.get(field) else ""
        for field in ("category_large", "category_middle", "category_small")
    )


def convert_erp_transactions(
    detection: ERPDetectionResult,
    *,
    mapping_overrides: Mapping[str, str] | None = None,
    start_date: object | None = None,
) -> pd.DataFrame:
    """将已确认ERP流水转换为FIFO标准流水，并统一数量符号与方向。"""
    mapping = _selected_mapping(
        detection, mapping_overrides, TRANSACTION_FIELDS
    )
    frame = _load_detected_frame(detection)
    rows: list[dict[str, object]] = []
    lookup: dict[tuple[str, str], dict[str, set[str]]] = {}
    for index, row in frame.iterrows():
        source_row = int(index) + 2
        date = pd.to_datetime(row[mapping["date"]], errors="coerce")
        if pd.isna(date):
            raise ValueError(f"ERP第{source_row}行日期为空或无效")
        warehouse = _text(row[mapping["warehouse"]])
        code = _text(row[mapping["material"]])
        name_column = mapping.get("material_name")
        name = _text(row[name_column]) if name_column else code
        if not warehouse or not code or not name:
            raise ValueError(f"ERP第{source_row}行仓库或物料为空")
        quantity = _decimal(row[mapping["quantity"]], "数量", source_row)
        direction = _direction(row[mapping["direction"]], source_row)
        category = _category(row, mapping)
        batch_column = mapping.get("batch")
        batch = _text(row[batch_column]) if batch_column else ""
        rows.append(
            {
                "仓库名称": warehouse,
                "物料名称": name,
                "品类": category,
                "方向": direction,
                "日期": pd.Timestamp(date),
                "数量": quantity,
                "批次号": f"{batch or 'ERP流水'}-{source_row}",
            }
        )
        entry = lookup.setdefault(
            (warehouse, code),
            {"names": set(), "categories": set()},
        )
        entry["names"].add(name)
        if category:
            entry["categories"].add(category)
    result = pd.DataFrame(rows, columns=STANDARD_TRANSACTION_COLUMNS)
    result.attrs.update(
        {
            "erp_material_lookup": lookup,
            "erp_mapping": mapping,
            "input_field_mapping": mapping,
            "source_file": str(detection.file_path),
            "source_sheet": detection.sheet_name,
            "requested_start_date": start_date,
        }
    )
    return normalize_transaction_quantity(result)


def convert_erp_opening_inventory(
    detection: ERPDetectionResult,
    transactions: pd.DataFrame,
    *,
    mapping_overrides: Mapping[str, str] | None = None,
    default_inventory_date: object | None = None,
) -> pd.DataFrame:
    """将ERP期初库存按仓库+物料编码连接到流水后转换为标准期初表。"""
    mapping = _selected_mapping(
        detection, mapping_overrides, OPENING_FIELDS
    )
    frame = _load_detected_frame(detection)
    quantity_probe = pd.DataFrame(index=frame.index)
    quantity_probe["仓库名称"] = frame[mapping["warehouse"]]
    quantity_probe["物料编码"] = frame[mapping["material"]]
    name_column = mapping.get("material_name")
    if name_column:
        quantity_probe["物料名称"] = frame[name_column]
    quantity_probe["期初数量"] = frame[mapping["quantity"]]
    quantity_probe.attrs["source_file"] = str(detection.file_path)
    quantity_probe.attrs["source_sheet"] = detection.sheet_name
    normalized_quantities = normalize_opening_inventory_quantity(
        quantity_probe,
        file_path=detection.file_path,
        sheet_name=detection.sheet_name,
    )
    valid_quantities = normalized_quantities["期初数量"].to_dict()
    frame = frame.loc[list(valid_quantities)].copy()
    lookup = transactions.attrs.get("erp_material_lookup", {})
    target_warehouses = set(transactions["仓库名称"].map(_text))
    rows: list[dict[str, object]] = []
    for index, row in frame.iterrows():
        source_row = int(index) + 2
        warehouse = _text(row[mapping["warehouse"]])
        if target_warehouses and warehouse not in target_warehouses:
            continue
        code = _text(row[mapping["material"]])
        quantity = valid_quantities[index]
        inventory_column = mapping.get("inventory_date")
        date_value = (
            row[inventory_column]
            if inventory_column
            else default_inventory_date
        )
        date = pd.to_datetime(date_value, errors="coerce")
        if pd.isna(date):
            raise ValueError(
                f"ERP第{source_row}行缺少有效库存日期/入库日期，"
                "且未提供期初基准日期"
            )
        match = lookup.get((warehouse, code))
        if not match or len(match["names"]) != 1:
            raise ValueError(
                f"ERP第{source_row}行物料编码{code}无法与流水唯一匹配"
            )
        supplied_name = _text(row[name_column]) if name_column else ""
        name = next(iter(match["names"]))
        if supplied_name and supplied_name != name:
            raise ValueError(
                f"ERP第{source_row}行物料编码{code}对应名称不一致："
                f"{supplied_name} != {name}"
            )
        categories = match["categories"]
        category = next(iter(categories)) if len(categories) == 1 else ""
        rows.append(
            {
                "仓库名称": warehouse,
                "物料名称": name,
                "品类": category,
                "期初数量": quantity,
                "期初批次入库基准日期": pd.Timestamp(date),
                "期初批次号": f"ERP期初-{source_row}",
            }
        )
    result = pd.DataFrame(rows, columns=STANDARD_OPENING_COLUMNS)
    result.attrs["source_file"] = str(detection.file_path)
    result.attrs["source_sheet"] = detection.sheet_name
    result.attrs["erp_mapping"] = mapping
    result.attrs["input_field_mapping"] = mapping
    result.attrs["opening_inventory_check"] = normalized_quantities.attrs[
        "opening_inventory_check"
    ]
    result.attrs["opening_inventory_negative_records"] = []
    snapshot_mode = (
        is_oracle_bi_html(detection.file_path)
        or "inventory_date" not in mapping
    )
    result["opening_date_type"] = "SNAPSHOT" if snapshot_mode else "REAL"
    result["original_inventory_date"] = result[
        "期初批次入库基准日期"
    ]
    result.attrs["opening_date_type"] = (
        "SNAPSHOT" if snapshot_mode else "REAL"
    )
    result.attrs["opening_inventory_source"] = (
        "Oracle BI Publisher HTML库存快照"
        if is_oracle_bi_html(detection.file_path)
        else ("ERP库存快照" if snapshot_mode else "ERP真实入库日期")
    )
    return result


def load_erp_inputs(
    transaction_path: str | Path,
    opening_path: str | Path,
    *,
    transaction_mapping: Mapping[str, str] | None = None,
    opening_mapping: Mapping[str, str] | None = None,
    period_start: object | None = None,
    mapping_path: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, ERPDetectionResult, ERPDetectionResult]:
    """检测并转换ERP流水、ERP期初库存，返回两个独立标准DataFrame。"""
    transaction_detection = detect_erp_file(
        transaction_path,
        table_kind="transaction",
        mapping_path=mapping_path,
    )
    transactions = convert_erp_transactions(
        transaction_detection,
        mapping_overrides=transaction_mapping,
        start_date=period_start,
    )
    opening_detection = detect_erp_file(
        opening_path,
        table_kind="opening",
        mapping_path=mapping_path,
    )
    openings = convert_erp_opening_inventory(
        opening_detection,
        transactions,
        mapping_overrides=opening_mapping,
        default_inventory_date=period_start,
    )
    return transactions, openings, transaction_detection, opening_detection
