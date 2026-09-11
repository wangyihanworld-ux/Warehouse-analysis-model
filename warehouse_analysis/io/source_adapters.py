from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pandas as pd

from warehouse_analysis.io.quantity_normalizer import (
    normalize_transaction_quantity,
)
from warehouse_analysis.io.opening_quantity_normalizer import (
    normalize_opening_inventory_quantity,
)


SICHUAN_WAREHOUSE = "示例原始仓库"
SICHUAN_OPENING_DATE = pd.Timestamp("2025-01-01")
SICHUAN_SNAPSHOT_NAME = "期初库存_20250101.xls"

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
    "期初批次号",
]
RAW_REQUIRED_COLUMNS = [
    "仓库名称",
    "物料名称",
    "EBS物料编码",
    "操作类型",
    "大类名称",
    "操作数量",
    "创建时间",
]

RUBBER_CATEGORIES = {"合成橡胶", "弹性体"}
RESIN_CATEGORIES = {
    "ABS",
    "AS树脂",
    "乙烯-醋酸乙烯共聚物",
    "其他树脂",
    "聚丙烯",
    "聚乙烯",
    "聚对苯二甲酸乙二酯（PET）",
    "聚烯烃弹性体",
    "聚苯乙烯",
    "色母",
}


class _OracleHtmlTableParser(HTMLParser):
    """读取Oracle BI Publisher生成的HTML伪xls表格。"""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if (
            tag in {"td", "th"}
            and self._cell is not None
            and self._row is not None
        ):
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif (
            tag == "tr"
            and self._row is not None
            and self._table is not None
        ):
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def _normalize_name(value: Any) -> str:
    """按已确认规则统一普通空格和不间断空格。"""
    if pd.isna(value):
        return ""
    return str(value).replace("\u00a0", " ").strip()


def _map_category(value: Any) -> str:
    """将示例原始大类映射为已确认的标准品类。"""
    if pd.isna(value):
        raise ValueError("大类名称为空")
    category = str(value).strip()
    if category in RUBBER_CATEGORIES:
        return "合成橡胶"
    if category in RESIN_CATEGORIES:
        return "合成树脂"
    raise ValueError(f"未配置的大类名称：{category}")


def _positive_decimal(value: Any, field: str) -> Decimal:
    """解析原始数量；不填充、不估算异常数据。"""
    if pd.isna(value):
        raise ValueError(f"{field}为空")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}不是有效数字：{value}") from exc
    if not result.is_finite():
        raise ValueError(f"{field}不是有限数字：{value}")
    return result


def _read_snapshot(path: Path) -> pd.DataFrame:
    """解析期初HTML伪xls并定位实际表头。"""
    if not path.is_file():
        raise FileNotFoundError(
            f"示例原始数据需要同目录期初文件：{path}"
        )
    parser = _OracleHtmlTableParser()
    parser.feed(path.read_text(encoding="utf-8"))
    required = {"子库名称", "物料编码", "物料描述", "期初数量"}
    for table in parser.tables:
        for header_index, row in enumerate(table):
            if required.issubset(set(row)):
                width = len(row)
                records = [
                    data_row[:width]
                    for data_row in table[header_index + 1 :]
                    if len(data_row) >= width
                ]
                frame = pd.DataFrame(records, columns=row)
                frame["_期初源数据行号"] = range(
                    header_index + 2,
                    header_index + 2 + len(frame),
                )
                return frame
    raise ValueError(
        "期初库存文件中未找到子库名称、物料编码、物料描述、期初数量字段"
    )


def load_sichuan_raw_data(
    raw_path: str | Path,
    snapshot_path: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """将示例原始仓库流水和期初快照转换为标准输入DataFrame。

    输入：
    - raw_path：包含“库存明细”Sheet的示例原始xlsx；
    - snapshot_path：可选Oracle HTML伪xls期初快照；省略时读取原始流水
      同目录下的“期初库存_20250101.xls”。

    输出：
    标准“出入库明细”和“期初库存”DataFrame。

    业务含义：
    复用已验证的EBS物料编码+标准化物料名称连接、品类映射和2025-01-01
    期初边界，将原始系统导出转换为V1.0引擎输入。

    FIFO影响：
    仅转换输入结构，不调用、不复制、不修改FIFO算法及费用计算逻辑。
    """
    raw_file = Path(raw_path)
    snapshot_file = (
        Path(snapshot_path)
        if snapshot_path is not None
        else raw_file.parent / SICHUAN_SNAPSHOT_NAME
    )
    if not raw_file.is_file():
        raise FileNotFoundError(f"示例原始流水文件不存在：{raw_file}")
    try:
        ledger = pd.read_excel(raw_file, sheet_name="库存明细")
    except ValueError as exc:
        raise ValueError(
            f"示例原始流水文件缺少“库存明细”工作表：{raw_file}"
        ) from exc
    missing = [
        column for column in RAW_REQUIRED_COLUMNS if column not in ledger.columns
    ]
    if missing:
        raise ValueError(
            f"示例原始流水缺少字段：{', '.join(missing)}"
        )

    ledger = ledger.copy()
    ledger["_源数据行号"] = ledger.index + 2
    ledger = ledger[
        ledger["仓库名称"].map(_normalize_name) == SICHUAN_WAREHOUSE
    ].copy()
    ledger["_物料名称"] = ledger["物料名称"].map(_normalize_name)
    ledger["_EBS物料编码"] = (
        ledger["EBS物料编码"].astype(str).str.strip()
    )
    ledger["_日期"] = pd.to_datetime(ledger["创建时间"], errors="coerce")
    bad_dates = ledger[ledger["_日期"].isna()]
    if not bad_dates.empty:
        rows = ", ".join(map(str, bad_dates["_源数据行号"].head(10)))
        raise ValueError(f"示例原始流水创建时间缺失或无效，源数据行：{rows}")
    ledger = ledger[ledger["_日期"] >= SICHUAN_OPENING_DATE].copy()

    unsupported = sorted(
        set(ledger["操作类型"].astype(str).str.strip()) - {"入库", "出库"}
    )
    if unsupported:
        raise ValueError(f"示例原始流水存在不支持的操作类型：{unsupported}")

    transaction_rows: list[dict[str, object]] = []
    for _, row in ledger.iterrows():
        quantity = _positive_decimal(row["操作数量"], "操作数量")
        transaction_rows.append(
            {
                "仓库名称": SICHUAN_WAREHOUSE,
                "物料名称": row["_物料名称"],
                "品类": _map_category(row["大类名称"]),
                "方向": str(row["操作类型"]).strip(),
                "日期": row["_日期"],
                "数量": float(quantity),
                "批次号": f"流水-{int(row['_源数据行号'])}",
            }
        )
    transactions = pd.DataFrame(transaction_rows, columns=TRANSACTION_COLUMNS)
    transactions.attrs["source_sheet"] = "库存明细"
    transactions = normalize_transaction_quantity(transactions)

    snapshot = _read_snapshot(snapshot_file)
    snapshot = snapshot[
        snapshot["子库名称"].map(_normalize_name) == SICHUAN_WAREHOUSE
    ].copy()
    names_by_code = (
        ledger.groupby("_EBS物料编码")["_物料名称"]
        .agg(lambda values: sorted(set(values)))
        .to_dict()
    )
    categories_by_pair = (
        ledger.groupby(["_EBS物料编码", "_物料名称"])["大类名称"]
        .agg(lambda values: sorted(set(map(str, values))))
        .to_dict()
    )
    opening_rows: list[dict[str, object]] = []
    errors: list[str] = []
    for _, row in snapshot.iterrows():
        source_row = int(row["_期初源数据行号"])
        code = str(row["物料编码"]).strip()
        name = _normalize_name(row["物料描述"])
        if code not in names_by_code:
            errors.append(f"第{source_row}行编码{code}在流水EBS物料编码中不存在")
            continue
        if name not in names_by_code[code]:
            errors.append(f"第{source_row}行编码存在但物料名称不一致：{name}")
            continue
        quantity = _positive_decimal(row["期初数量"], "期初数量")
        mapped_categories = {
            _map_category(category)
            for category in categories_by_pair.get((code, name), [])
        }
        if len(mapped_categories) != 1:
            errors.append(
                f"第{source_row}行无法确定唯一品类：{sorted(mapped_categories)}"
            )
            continue
        opening_rows.append(
            {
                "仓库名称": SICHUAN_WAREHOUSE,
                "物料名称": name,
                "品类": next(iter(mapped_categories)),
                "期初数量": float(quantity),
                "期初批次入库基准日期": SICHUAN_OPENING_DATE,
                "期初批次号": f"期初-{source_row}",
            }
        )
    openings = pd.DataFrame(opening_rows, columns=OPENING_COLUMNS)
    openings.attrs["source_file"] = str(snapshot_file)
    openings.attrs["source_sheet"] = "Oracle HTML期初库存"
    openings = normalize_opening_inventory_quantity(
        openings,
        file_path=snapshot_file,
        sheet_name="Oracle HTML期初库存",
    )
    openings["opening_date_type"] = "SNAPSHOT"
    openings["original_inventory_date"] = (
        SICHUAN_OPENING_DATE - pd.Timedelta(days=1)
    )
    openings.attrs["opening_date_type"] = "SNAPSHOT"
    openings.attrs[
        "opening_inventory_source"
    ] = "Oracle BI Publisher HTML库存快照"
    if errors:
        preview = "；".join(errors[:10])
        raise ValueError(
            f"示例期初快照存在{len(errors)}条无法安全匹配的记录：{preview}"
        )
    return transactions, openings
