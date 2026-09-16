from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pandas as pd

from warehouse_analysis.core.validation import _require_columns
from warehouse_analysis.io.source_adapters import _OracleHtmlTableParser
from warehouse_analysis.io.quantity_normalizer import (
    normalize_transaction_quantity,
)
from warehouse_analysis.io.opening_quantity_normalizer import (
    OpeningFieldAmbiguityError,
    find_opening_quantity_candidates,
    normalize_opening_inventory_quantity,
)
from warehouse_analysis.io.category_normalizer import standardize_category_parts


TRANSACTION_COLUMNS = [
    "仓库名称",
    "物料名称",
    "品类",
    "方向",
    "日期",
    "数量",
    "批次号",
]
OPENING_COLUMNS = [
    "仓库名称",
    "物料名称",
    "品类",
    "期初数量",
    "期初批次入库基准日期",
]
RATE_COLUMNS = ["仓库名称", "品类或物料", "单价", "计费方式说明"]

TRANSACTION_FIELD_ALIASES = {
    "仓库名称": ("仓库名称", "仓库", "库房名称"),
    "物料名称": ("物料名称", "物料描述", "商品名称"),
    "品类": ("品类", "大类名称", "物料类别"),
    "方向": ("方向", "操作类型", "出入库方向"),
    "日期": ("日期", "创建时间", "操作日期", "出入库日期"),
    "数量": ("数量", "操作数量", "重量(吨)", "库存数量"),
    "批次号": ("批次号", "批号", "单据号", "关联单号"),
}
CATEGORY_LEVEL_ALIASES = {
    "large": ("大类名称", "一级品类", "品类大类"),
    "middle": ("中类名称", "二级品类", "品类中类"),
    "small": ("小类名称", "三级品类", "品类小类"),
}
OPENING_FIELD_ALIASES = {
    "仓库名称": (
        "仓库名称",
        "仓库",
        "库房名称",
        "库存组织名称",
        "子库名称",
    ),
    "物料编码": ("EBS物料编码", "物料编码", "物料号"),
    "物料名称": ("物料名称", "物料描述", "商品名称"),
    "品类": ("品类", "大类名称", "物料类别"),
    "期初数量": (
        "期初数量",
        "期初库存",
        "期初库存数量",
        "初始库存",
        "初始数量",
        "库存数量",
        "库存余额",
        "可用数量",
        "现存量",
        "数量",
    ),
    "期初批次入库基准日期": (
        "期初批次入库基准日期",
        "期初日期",
        "库存日期",
        "截止日期",
        "盘点日期",
        "查询日期",
        "数据日期",
        "创建日期",
        "入库日期",
        "交易日期",
    ),
    "期初批次号": ("期初批次号", "批次号", "批号"),
}


class InputWorkbookError(ValueError):
    """面向业务人员的Excel输入错误。

    输入：文件、工作表、具体错误和修复建议。
    输出：可直接展示在CLI或GUI中的结构化中文错误信息。
    业务含义：在保留原始异常链的同时说明错误位置和处理方法。
    FIFO影响：仅在计算前校验输入，不改变任何有效数据或FIFO结果。
    """

    def __init__(
        self,
        *,
        file_path: Path,
        sheet_name: str,
        error: str,
        suggestion: str,
        format_name: str | None = None,
    ) -> None:
        self.file_path = Path(file_path)
        self.sheet_name = sheet_name
        self.error = error
        self.suggestion = suggestion
        self.format_name = format_name
        location = (
            f"格式：{format_name}\n"
            if format_name
            else f"Sheet：{self.sheet_name}\n"
        )
        issue_label = "问题" if format_name else "错误"
        super().__init__(
            f"文件：{self.file_path}\n"
            f"{location}"
            f"{issue_label}：{self.error}\n"
            f"建议：{self.suggestion}"
        )


def is_oracle_bi_html(path: str | Path) -> bool:
    """按文件头识别Oracle BI Publisher HTML伪xls，不依赖扩展名。"""
    source = Path(path)
    if not source.is_file():
        return False
    try:
        header = source.read_bytes()[:65536].lower()
    except OSError:
        return False
    return b"<html" in header and b"<table" in header


def _html_snapshot_date(parser: _OracleHtmlTableParser) -> pd.Timestamp | None:
    """从Oracle报表参数区读取快照结束日期；期初基准日为其下一日。"""
    for table in parser.tables:
        for row in table:
            cells = [str(value).replace("\u00a0", " ").strip() for value in row]
            for label in ("结束日期", "库存日期"):
                if label not in cells:
                    continue
                index = cells.index(label)
                if index + 1 < len(cells):
                    value = pd.to_datetime(cells[index + 1], errors="coerce")
                    if not pd.isna(value):
                        return pd.Timestamp(value).normalize()
    return None


def _detect_oracle_html_opening_table(
    path: Path,
) -> tuple[pd.DataFrame, str, dict[str, str]]:
    """解析多HTML表并定位包含仓库、物料、数量的期初库存表。"""
    parser = _OracleHtmlTableParser()
    try:
        parser.feed(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise InputWorkbookError(
            file_path=path,
            sheet_name="",
            format_name="Oracle BI Publisher HTML报表",
            error="HTML报表无法读取或编码无效",
            suggestion="请重新从Oracle BI Publisher导出期初库存报表。",
        ) from exc

    required = ["仓库名称", "物料名称", "期初数量"]
    matches: list[tuple[pd.DataFrame, str, dict[str, str]]] = []
    best_mapping: dict[str, str] = {}
    for table_number, table in enumerate(parser.tables, start=1):
        for header_index, row in enumerate(table):
            mapping = {
                standard: source
                for standard, choices in OPENING_FIELD_ALIASES.items()
                if (source := _find_alias(list(row), choices)) is not None
            }
            if len(mapping) > len(best_mapping):
                best_mapping = mapping
            if not all(field in mapping for field in required):
                continue
            width = len(row)
            records = [
                data_row[:width]
                for data_row in table[header_index + 1 :]
                if len(data_row) >= width
            ]
            if records:
                matches.append(
                    (
                        pd.DataFrame(records, columns=row),
                        f"HTML表{table_number}",
                        mapping,
                    )
                )
            break
    if not matches:
        missing_groups = [
            label
            for field, label in (
                ("仓库名称", "仓库"),
                ("物料名称", "物料"),
                ("期初数量", "数量"),
            )
            if field not in best_mapping
        ]
        raise InputWorkbookError(
            file_path=path,
            sheet_name="",
            format_name="Oracle BI Publisher HTML报表",
            error=(
                "未找到包含期初库存字段的数据表\n"
                f"缺失：{'/'.join(missing_groups) if missing_groups else '仓库/物料/数量'}"
            ),
            suggestion="确认导出的库存报表包含完整字段。",
        )
    if len(matches) > 1:
        raise InputWorkbookError(
            file_path=path,
            sheet_name="",
            format_name="Oracle BI Publisher HTML报表",
            error="发现多个包含期初库存字段的数据表，无法唯一确定目标表",
            suggestion="请仅保留目标期初库存表，或分别导出后再导入。",
        )
    frame, table_name, mapping = matches[0]
    if "期初批次入库基准日期" not in mapping:
        snapshot_date = _html_snapshot_date(parser)
        if snapshot_date is None:
            raise InputWorkbookError(
                file_path=path,
                sheet_name="",
                format_name="Oracle BI Publisher HTML报表",
                error="已找到期初库存表，但报表参数区缺少有效的结束日期/库存日期",
                suggestion="请确认报表头包含快照日期，或使用标准期初库存模板。",
            )
        column = "__Oracle期初批次入库基准日期"
        frame[column] = snapshot_date + pd.Timedelta(days=1)
        frame.attrs["oracle_snapshot_date"] = snapshot_date
        mapping["期初批次入库基准日期"] = column
    return frame, table_name, mapping


def _read_sheet(
    path: Path,
    sheet_name: str,
    required_columns: list[str],
    table_name: str,
) -> pd.DataFrame:
    """读取并检查单张业务表，错误时增加文件和Sheet上下文。"""
    try:
        frame = pd.read_excel(path, sheet_name=sheet_name)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise InputWorkbookError(
            file_path=path,
            sheet_name=sheet_name,
            error=str(exc),
            suggestion=f"请确认文件存在，并包含名为“{sheet_name}”的工作表。",
        ) from exc
    try:
        _require_columns(frame, required_columns, table_name)
    except ValueError as exc:
        missing = [
            column for column in required_columns if column not in frame.columns
        ]
        raise InputWorkbookError(
            file_path=path,
            sheet_name=sheet_name,
            error=f"缺少字段：{', '.join(missing)}",
            suggestion=f"请补充{', '.join(missing)}列，并保持列名完全一致。",
        ) from exc
    return frame


def _workbook_sheets(path: Path) -> list[str]:
    """返回可读取工作簿的Sheet列表，并包装为业务错误。"""
    try:
        with pd.ExcelFile(path) as workbook:
            return workbook.sheet_names
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise InputWorkbookError(
            file_path=path,
            sheet_name="工作簿",
            error=str(exc),
            suggestion="请确认文件存在且为可正常打开的Excel工作簿。",
        ) from exc


def _find_alias(
    columns: list[object], aliases: tuple[str, ...]
) -> str | None:
    normalized = {str(column).strip(): str(column) for column in columns}
    return next(
        (normalized[alias] for alias in aliases if alias in normalized),
        None,
    )


def _detect_table(
    path: Path,
    aliases: dict[str, tuple[str, ...]],
    required: list[str],
    table_name: str,
) -> tuple[pd.DataFrame, str, dict[str, str]]:
    """跨任意Sheet识别字段，并返回原表、Sheet名和标准字段映射。"""
    candidates: list[tuple[pd.DataFrame, str, dict[str, str]]] = []
    inspected: list[tuple[str, list[str]]] = []
    for sheet_name in _workbook_sheets(path):
        try:
            frame = pd.read_excel(path, sheet_name=sheet_name)
        except (ValueError, OSError) as exc:
            raise InputWorkbookError(
                file_path=path,
                sheet_name=sheet_name,
                error=str(exc),
                suggestion="请确认该工作表可以正常读取。",
            ) from exc
        inspected.append((sheet_name, list(map(str, frame.columns))))
        mapping = {
            standard: source
            for standard, choices in aliases.items()
            if (source := _find_alias(list(frame.columns), choices)) is not None
        }
        if all(field in mapping for field in required):
            candidates.append((frame, sheet_name, mapping))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = "、".join(candidate[1] for candidate in candidates)
        raise InputWorkbookError(
            file_path=path,
            sheet_name=names,
            error=f"发现多个可识别的{table_name}工作表，无法自动确定。",
            suggestion=f"请仅保留一张{table_name}表，或使用标准模板。",
        )
    best_sheet, best_columns = max(
        inspected,
        key=lambda item: sum(
            _find_alias(item[1], aliases[field]) is not None
            for field in required
        ),
        default=("工作簿", []),
    )
    missing = [
        field
        for field in required
        if _find_alias(best_columns, aliases[field]) is None
    ]
    raise InputWorkbookError(
        file_path=path,
        sheet_name=best_sheet,
        error=f"缺少字段：{', '.join(missing)}",
        suggestion=f"请补充{', '.join(missing)}字段后重新导入。",
    )


def load_transaction_file(path: str | Path) -> pd.DataFrame:
    """从任意文件名和Sheet名中识别并标准化出入库明细字段。

    只做列名映射；数据值仍交给既有validation层校验，不影响FIFO口径。
    """
    source = Path(path)
    required = ["仓库名称", "物料名称", "品类", "方向", "日期", "数量"]
    frame, sheet_name, mapping = _detect_table(
        source,
        TRANSACTION_FIELD_ALIASES,
        required,
        "出入库明细",
    )
    result = frame.rename(
        columns={source_name: standard for standard, source_name in mapping.items()}
    ).copy()
    source_columns = list(frame.columns)
    category_columns = [
        mapping.get("品类"),
        _find_alias(source_columns, CATEGORY_LEVEL_ALIASES["middle"]),
        _find_alias(source_columns, CATEGORY_LEVEL_ALIASES["small"]),
    ]
    if any(category_columns[1:]):
        result["品类"] = [
            standardize_category_parts(
                frame.loc[index, column] for column in category_columns if column
            )
            for index in frame.index
        ]
    else:
        result["品类"] = result["品类"].map(
            lambda value: standardize_category_parts([value])
        )
    if "批次号" not in result.columns:
        result["批次号"] = [
            f"流水-{sheet_name}-{row_number}"
            for row_number in range(2, len(result) + 2)
        ]
    result.attrs["source_sheet"] = sheet_name
    result.attrs["source_file"] = str(source)
    result.attrs["input_field_mapping"] = dict(mapping)
    selected = result[TRANSACTION_COLUMNS].copy()
    selected.attrs.update(result.attrs)
    return normalize_transaction_quantity(selected)


def load_opening_inventory_file(
    path: str | Path,
    transactions: pd.DataFrame | None = None,
    *,
    default_inventory_date: object | None = None,
    field_mapping: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """从独立文件任意Sheet识别期初库存，并转换为核心标准字段。

    若期初文件没有品类，仅在流水中同一仓库+物料名称对应唯一品类时补齐；
    不能唯一确定时明确报错，不猜测、不估算。
    """
    source = Path(path)
    required = [
        "仓库名称",
        "物料名称",
        "期初数量",
    ]
    if is_oracle_bi_html(source):
        frame, sheet_name, mapping = _detect_oracle_html_opening_table(source)
    else:
        frame, sheet_name, mapping = _detect_table(
            source,
            OPENING_FIELD_ALIASES,
            required,
            "期初库存",
        )
        overrides = dict(field_mapping or {})
        quantity_candidates = find_opening_quantity_candidates(
            list(frame.columns)
        )
        selected_quantity = overrides.get("期初数量")
        if selected_quantity:
            if selected_quantity not in quantity_candidates:
                raise InputWorkbookError(
                    file_path=source,
                    sheet_name=sheet_name,
                    error=f"选择的期初数量字段不存在：{selected_quantity}",
                    suggestion="请重新选择候选数量字段。",
                )
            mapping["期初数量"] = selected_quantity
        elif len(quantity_candidates) > 1:
            raise OpeningFieldAmbiguityError(
                file_path=str(source),
                sheet_name=sheet_name,
                field_name="期初数量",
                candidates=quantity_candidates,
            )
        date_aliases = OPENING_FIELD_ALIASES["期初批次入库基准日期"]
        date_candidates = [
            str(column)
            for alias in date_aliases
            for column in frame.columns
            if str(column).strip() == alias
        ]
        selected_date = overrides.get("期初批次入库基准日期")
        if selected_date:
            if selected_date not in date_candidates:
                raise InputWorkbookError(
                    file_path=source,
                    sheet_name=sheet_name,
                    error=f"选择的期初日期字段不存在：{selected_date}",
                    suggestion="请重新选择候选日期字段。",
                )
            mapping["期初批次入库基准日期"] = selected_date
        elif len(date_candidates) > 1:
            raise OpeningFieldAmbiguityError(
                file_path=str(source),
                sheet_name=sheet_name,
                field_name="期初日期",
                candidates=date_candidates,
            )
    snapshot_mode = "期初批次入库基准日期" not in mapping
    if snapshot_mode:
        if default_inventory_date is None:
            raise InputWorkbookError(
                file_path=source,
                sheet_name=sheet_name,
                error="缺少字段：期初批次入库基准日期；且未提供统计开始日期",
                suggestion="请在运行时明确填写统计开始日期。",
            )
        synthetic_date = "__统计开始日期"
        frame[synthetic_date] = pd.Timestamp(default_inventory_date)
        mapping["期初批次入库基准日期"] = synthetic_date
    result = frame.rename(
        columns={source_name: standard for standard, source_name in mapping.items()}
    ).copy()
    for column in ("仓库名称", "物料名称"):
        if column in result.columns:
            result[column] = (
                result[column]
                .fillna("")
                .astype(str)
                .str.replace("\u00a0", " ", regex=False)
                .str.strip()
            )
    if is_oracle_bi_html(source) and transactions is not None:
        target_warehouses = {
            str(value).replace("\u00a0", " ").strip()
            for value in transactions["仓库名称"]
            if not pd.isna(value)
        }
        result = result[result["仓库名称"].isin(target_warehouses)].copy()
    result.attrs["source_file"] = str(source)
    result.attrs["source_sheet"] = sheet_name
    result.attrs["input_field_mapping"] = dict(mapping)
    result = normalize_opening_inventory_quantity(
        result,
        file_path=source,
        sheet_name=sheet_name,
    )
    if transactions is not None:
        category_map = (
            transactions.groupby(["仓库名称", "物料名称"])["品类"]
            .agg(lambda values: sorted(set(map(str, values))))
            .to_dict()
        )
        categories: list[str] = []
        unresolved: list[int] = []
        for index, row in result.iterrows():
            matches = category_map.get(
                (str(row["仓库名称"]).strip(), str(row["物料名称"]).strip()),
                [],
            )
            if len(matches) == 1:
                # 期初库存来源可能把三级品类写成“一级.二级.三级”。
                # 同一仓库+物料在流水中已有唯一标准品类时，直接复用该值，
                # 保证两条输入适配路径在进入FIFO前使用同一标准字符串。
                categories.append(matches[0])
            elif "品类" not in result.columns:
                unresolved.append(int(index) + 2)
                categories.append("")
            else:
                categories.append(str(row["品类"]).strip())
        if unresolved:
            preview = "、".join(map(str, unresolved[:10]))
            raise InputWorkbookError(
                file_path=source,
                sheet_name=sheet_name,
                error=f"缺少品类且无法由流水唯一校验，数据行：{preview}",
                suggestion="请补充品类字段，或确保仓库名称+物料名称在流水中只对应一个品类。",
            )
        result["品类"] = categories
    elif "品类" not in result.columns:
        raise InputWorkbookError(
            file_path=source,
            sheet_name=sheet_name,
            error="缺少字段：品类",
            suggestion="请补充品类字段后重新导入。",
        )
    if "期初批次号" not in result.columns:
        result["期初批次号"] = [
            f"期初-{sheet_name}-{row_number}"
            for row_number in range(2, len(result) + 2)
        ]
    if is_oracle_bi_html(source) or snapshot_mode:
        result["opening_date_type"] = "SNAPSHOT"
        result["original_inventory_date"] = (
            frame.attrs.get(
                "oracle_snapshot_date",
                pd.to_datetime(
                    result["期初批次入库基准日期"], errors="coerce"
                ) - pd.Timedelta(days=1),
            )
            if is_oracle_bi_html(source)
            else pd.NaT
        )
        result.attrs["opening_date_type"] = "SNAPSHOT"
        result.attrs[
            "opening_inventory_source"
        ] = (
            "Oracle BI Publisher HTML库存快照"
            if is_oracle_bi_html(source)
            else "ERP库存快照"
        )
    else:
        result["opening_date_type"] = "REAL"
        result["original_inventory_date"] = result[
            "期初批次入库基准日期"
        ]
        result.attrs["opening_date_type"] = "REAL"
        result.attrs["opening_inventory_source"] = "标准期初库存文件"
    selected = result[
        OPENING_COLUMNS
        + ["期初批次号", "opening_date_type", "original_inventory_date"]
    ].copy()
    selected.attrs.update(result.attrs)
    return selected


def load_separate_business_files(
    transaction_path: str | Path,
    opening_inventory_path: str | Path,
    *,
    default_inventory_date: object | None = None,
    opening_field_mapping: Mapping[str, str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """独立读取流水和期初库存，保持两个DataFrame分别进入核心。"""
    transactions = load_transaction_file(transaction_path)
    openings = load_opening_inventory_file(
        opening_inventory_path,
        transactions,
        default_inventory_date=default_inventory_date,
        field_mapping=opening_field_mapping,
    )
    return transactions, openings


def _select_opening_sheet(path: Path) -> str:
    """按“期初库存”优先、兼容“期初存货”的规则选择Sheet。"""
    try:
        with pd.ExcelFile(path) as workbook:
            sheet_names = workbook.sheet_names
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise InputWorkbookError(
            file_path=path,
            sheet_name="工作簿",
            error=str(exc),
            suggestion="请确认文件存在且为可正常打开的Excel工作簿。",
        ) from exc
    has_inventory = "期初库存" in sheet_names
    has_stock = "期初存货" in sheet_names
    if has_inventory and has_stock:
        raise InputWorkbookError(
            file_path=path,
            sheet_name="期初库存 / 期初存货",
            error="同时存在“期初库存”和“期初存货”两个工作表，无法确定使用哪一个。",
            suggestion="请只保留一个期初工作表；新模板建议使用“期初库存”。",
        )
    if has_inventory:
        return "期初库存"
    if has_stock:
        return "期初存货"
    raise InputWorkbookError(
        file_path=path,
        sheet_name="期初库存",
        error="未找到“期初库存”或“期初存货”工作表。",
        suggestion="请新增“期初库存”工作表，并按模板填写必填字段。",
    )


def load_business_workbook(
    path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """读取业务Excel中的出入库明细和期初库存，不读取费率。"""
    path = Path(path)
    transactions = _read_sheet(
        path, "出入库明细", TRANSACTION_COLUMNS, "出入库明细"
    )
    transactions.attrs["source_sheet"] = "出入库明细"
    transactions = normalize_transaction_quantity(transactions)
    opening_sheet = _select_opening_sheet(path)
    openings = _read_sheet(
        path, opening_sheet, OPENING_COLUMNS, opening_sheet
    )
    openings.attrs["source_file"] = str(path)
    openings.attrs["source_sheet"] = opening_sheet
    openings = normalize_opening_inventory_quantity(
        openings,
        file_path=path,
        sheet_name=opening_sheet,
    )
    openings["opening_date_type"] = "REAL"
    openings["original_inventory_date"] = openings[
        "期初批次入库基准日期"
    ]
    openings.attrs["opening_date_type"] = "REAL"
    openings.attrs["opening_inventory_source"] = "标准期初库存文件"
    return transactions, openings


def load_rate_config(path: Path) -> pd.DataFrame:
    """读取独立费率配置。

    独立文件优先读取“计价规则”Sheet；若文件只有一个Sheet，则使用该Sheet。
    只执行字段检查，费率数值及生效日期仍由validation模块按原口径处理。
    """
    path = Path(path)
    try:
        with pd.ExcelFile(path) as workbook:
            sheet_names = workbook.sheet_names
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise InputWorkbookError(
            file_path=path,
            sheet_name="计价规则",
            error=str(exc),
            suggestion="请确认费率配置文件存在且为可正常打开的Excel工作簿。",
        ) from exc
    if "计价规则" in sheet_names:
        sheet_name = "计价规则"
    elif len(sheet_names) == 1:
        sheet_name = sheet_names[0]
    else:
        raise InputWorkbookError(
            file_path=path,
            sheet_name="计价规则",
            error="未找到“计价规则”工作表，且文件包含多个其他工作表。",
            suggestion="请将费率配置工作表命名为“计价规则”。",
        )
    return _read_sheet(path, sheet_name, RATE_COLUMNS, "计价规则")


def load_analysis_inputs(
    business_path: Path,
    rate_config_path: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """统一读取新旧两种输入模式。

    输入：业务Excel路径和可选独立费率配置路径。
    输出：出入库、期初库存、计价规则三张DataFrame。
    业务含义：有独立费率文件时使用新模式，否则使用历史三表合一模式。
    FIFO影响：只改变文件组织方式，不改变DataFrame内容和FIFO口径。
    """
    transactions, openings = load_business_workbook(Path(business_path))
    if rate_config_path is not None:
        rates = load_rate_config(Path(rate_config_path))
    else:
        rates = _read_sheet(
            Path(business_path),
            "计价规则",
            RATE_COLUMNS,
            "计价规则",
        )
    return transactions, openings, rates


def load_input_workbook(
    path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """读取旧版三表合一输入工作簿。

    输入：包含出入库明细、期初存货和计价规则的xlsx路径。
    输出：三张未经标准化的DataFrame。
    业务含义：保持第二阶段旧输入协议和缺列报错完全兼容。
    FIFO影响：只负责读取，不改变数据内容或FIFO结果。
    """
    return load_analysis_inputs(Path(path), rate_config_path=None)
