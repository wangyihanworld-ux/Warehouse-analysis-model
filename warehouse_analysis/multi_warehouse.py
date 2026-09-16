from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Callable, Mapping, Sequence

import pandas as pd

from warehouse_analysis.analysis.company_summary import build_company_summary
from warehouse_analysis.history_db import (
    create_run,
    finish_run,
    save_error,
    save_rate_snapshot,
    save_warehouse_result,
)
from warehouse_analysis.io.erp_adapter import load_erp_inputs
from warehouse_analysis.io.excel_loader import load_separate_business_files
from warehouse_analysis.logging_config import configure_logging
from warehouse_analysis.rate_manager import (
    DEFAULT_RATE_DATABASE,
    get_effective_rate_rule,
    search_rate_rule,
)
from warehouse_analysis.reporting.file_naming import (
    ensure_unique_report_path,
    resolve_report_output_path,
)
from warehouse_analysis.reporting.company_report import build_company_report
from warehouse_analysis.service import _coerce_period, run_analysis
from warehouse_analysis.version import VERSION


@dataclass(frozen=True)
class MultiWarehouseResult:
    """企业多仓任务的调度结果。"""

    records: list[dict[str, object]]
    company_summary: pd.DataFrame
    company_report_path: Path
    run_id: int

    @property
    def success_count(self) -> int:
        return sum(record["status"] == "success" for record in self.records)

    @property
    def failure_count(self) -> int:
        return sum(record["status"] == "failed" for record in self.records)


def _safe_folder_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", value).strip().rstrip(".")
    return cleaned or "未命名仓库"


def _period_folder(start: pd.Timestamp, end: pd.Timestamp, name: str) -> str:
    if start.year == end.year and start.month == end.month:
        return start.strftime("%Y-%m")
    return _safe_folder_name(name)


def identify_warehouses(
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
) -> list[str]:
    """从标准DataFrame识别仓库，并验证期初库存不会跨仓混用。"""
    transaction_warehouses = {
        str(value).strip()
        for value in transactions["仓库名称"]
        if str(value).strip()
    }
    opening_warehouses = {
        str(value).strip()
        for value in openings["仓库名称"]
        if str(value).strip()
    }
    return sorted(transaction_warehouses | opening_warehouses)


def find_effective_rate_selection(
    warehouse: str,
    stat_date: object,
    rate_database_path: str | Path = DEFAULT_RATE_DATABASE,
) -> tuple[str, pd.DataFrame, pd.Timestamp]:
    """为仓库查找唯一有效费率；禁止借用其他仓库规则。"""
    choices = search_rate_rule(warehouse, rate_database_path)
    choices = choices[choices["仓库名称"] == warehouse]
    valid: list[tuple[str, pd.DataFrame, pd.Timestamp]] = []
    for selection in choices["选择项"]:
        try:
            rates, version = get_effective_rate_rule(
                selection, stat_date, rate_database_path
            )
        except ValueError:
            continue
        valid.append((str(selection), rates, version))
    if not valid:
        raise ValueError(
            f"仓库：{warehouse}\n未找到有效费率规则\n请进入费率管理维护"
        )
    if len(valid) > 1:
        names = "、".join(item[0] for item in valid)
        raise ValueError(
            f"仓库：{warehouse}\n存在多个有效费率规则：{names}\n"
            "请在费率管理中停用不适用规则，确保批量运行只有一个有效选择项"
        )
    return valid[0]


def _excel_safe(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in result.columns:
        if result[column].map(lambda value: isinstance(value, Decimal)).any():
            result[column] = result[column].map(
                lambda value: float(value)
                if isinstance(value, Decimal)
                else value
            )
    return result


def _write_standard_input(
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
    path: Path,
) -> None:
    """仅序列化标准DataFrame，供既有文件型service入口消费。"""
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _excel_safe(transactions).to_excel(
            writer, sheet_name="出入库明细", index=False
        )
        _excel_safe(openings).to_excel(
            writer, sheet_name="期初库存", index=False
        )


def run_multi_warehouse_analysis(
    input_file: str | Path,
    opening_inventory_file: str | Path,
    period,
    output_directory: str | Path,
    *,
    input_type: str = "erp",
    selected_warehouses: Sequence[str] | None = None,
    rate_database_path: str | Path = DEFAULT_RATE_DATABASE,
    history_database_path: str | Path | None = None,
    erp_transaction_mapping: Mapping[str, str] | None = None,
    erp_opening_mapping: Mapping[str, str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> MultiWarehouseResult:
    """识别、拆分并逐仓调用service.run_analysis，最后生成企业汇总。

    本函数不包含FIFO、费率匹配算法或费用公式。
    """
    logger = configure_logging()
    stat_period = _coerce_period(period)
    started = perf_counter()

    def progress(message: str) -> None:
        logger.info(message)
        if progress_callback:
            progress_callback(message)

    run_id = create_run(
        version=VERSION,
        run_type="企业多仓批量分析",
        input_file=input_file,
        opening_file=opening_inventory_file,
        start_date=stat_period.start.date(),
        end_date=stat_period.end.date(),
        path=history_database_path,
    )
    progress("开始多仓批量分析")
    try:
        if input_type == "erp":
            transactions, openings, _, _ = load_erp_inputs(
                input_file,
                opening_inventory_file,
                transaction_mapping=erp_transaction_mapping,
                opening_mapping=erp_opening_mapping,
                period_start=stat_period.start,
            )
        elif input_type == "separate":
            transactions, openings = load_separate_business_files(
                input_file, opening_inventory_file
            )
        else:
            raise ValueError("多仓模式仅支持erp或separate输入类型")
    except Exception as exc:
        save_error(
            run_id=run_id,
            warehouse="",
            module="多仓输入适配",
            error_type=exc.__class__.__name__,
            description=str(exc),
            suggestion="检查ERP字段映射、期初库存及文件格式。",
            path=history_database_path,
        )
        finish_run(
            run_id,
            "failed",
            perf_counter() - started,
            history_database_path,
        )
        raise

    warehouses = identify_warehouses(transactions, openings)
    if selected_warehouses is not None:
        requested = {str(value).strip() for value in selected_warehouses}
        warehouses = [warehouse for warehouse in warehouses if warehouse in requested]
    if not warehouses:
        raise ValueError("没有可运行的仓库任务")
    progress(f"发现仓库数量：{len(warehouses)}")

    period_directory = (
        Path(output_directory)
        / "reports"
        / _period_folder(stat_period.start, stat_period.end, stat_period.name)
    )
    records: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="warehouse_multi_") as temporary:
        temp_root = Path(temporary)
        for task_number, warehouse in enumerate(warehouses, start=1):
            progress(f"开始：{warehouse}")
            warehouse_transactions = transactions[
                transactions["仓库名称"].astype(str).str.strip() == warehouse
            ].copy()
            warehouse_openings = openings[
                openings["仓库名称"].astype(str).str.strip() == warehouse
            ].copy()
            report_path = resolve_report_output_path(
                period_directory / _safe_folder_name(warehouse),
                warehouse_name=warehouse,
                start_date=stat_period.start,
                end_date=stat_period.end,
                version=VERSION,
            )
            record: dict[str, object] = {
                "warehouse": warehouse,
                "period": stat_period.name,
                "report_path": str(report_path),
                "report_filename": report_path.name,
            }
            if warehouse_transactions.empty or warehouse_openings.empty:
                missing = (
                    "出入库流水"
                    if warehouse_transactions.empty
                    else "期初库存"
                )
                message = f"仓库{warehouse}缺少{missing}"
                record.update(
                    {
                        "status": "failed",
                        "error": message,
                        "suggestion": f"请补充该仓库的{missing}。",
                    }
                )
                save_error(
                    run_id=run_id,
                    warehouse=warehouse,
                    module="仓库任务拆分",
                    error_type="MissingWarehouseData",
                    description=message,
                    suggestion=record["suggestion"],
                    path=history_database_path,
                )
                save_warehouse_result(
                    run_id,
                    warehouse=warehouse,
                    period=stat_period.name,
                    ending_inventory=0,
                    average_inventory=0,
                    average_days=0,
                    fee=0,
                    report_path=report_path,
                    status="failed",
                    path=history_database_path,
                )
                records.append(record)
                progress(f"{warehouse}失败 | 原因：{message}")
                continue
            try:
                selection, rates, rate_version = find_effective_rate_selection(
                    warehouse, stat_period.end, rate_database_path
                )
                temp_input = temp_root / f"task_{task_number}.xlsx"
                _write_standard_input(
                    warehouse_transactions, warehouse_openings, temp_input
                )
                result = run_analysis(
                    temp_input,
                    None,
                    stat_period,
                    report_path,
                    input_type="standard",
                    rate_selection=selection,
                    rate_database_path=rate_database_path,
                )
                if not result.success:
                    raise RuntimeError(result.error or "单仓服务运行失败")
                report_path = result.output_path or report_path
                record.update(
                    {
                        "status": "success",
                        "report_path": str(report_path),
                        "report_filename": report_path.name,
                        "rate_selection": selection,
                        "rate_version": rate_version,
                        "summary": result.summary,
                    }
                )
                save_warehouse_result(
                    run_id,
                    warehouse=warehouse,
                    period=stat_period.name,
                    ending_inventory=float(
                        result.summary["期末库存量（吨）"]
                    ),
                    average_inventory=float(
                        result.summary.get("平均库存量（吨）", 0)
                    ),
                    average_days=float(
                        result.summary.get("平均存放天数", 0)
                    ),
                    fee=float(result.summary["仓储费总额（元）"]),
                    report_path=report_path,
                    path=history_database_path,
                )
                for price in sorted(set(map(float, rates["单价"]))):
                    save_rate_snapshot(
                        run_id,
                        warehouse=warehouse,
                        rule=selection,
                        version_date=rate_version,
                        price=price,
                        path=history_database_path,
                    )
                progress(f"{warehouse}完成 | 报告={report_path.name}")
            except Exception as exc:
                record.update(
                    {
                        "status": "failed",
                        "error": str(exc),
                        "suggestion": (
                            "请检查该仓库费率规则、输入数据和日志。"
                        ),
                    }
                )
                save_error(
                    run_id=run_id,
                    warehouse=warehouse,
                    module="单仓分析调度",
                    error_type=exc.__class__.__name__,
                    description=str(exc),
                    suggestion=record["suggestion"],
                    path=history_database_path,
                )
                save_warehouse_result(
                    run_id,
                    warehouse=warehouse,
                    period=stat_period.name,
                    ending_inventory=0,
                    average_inventory=0,
                    average_days=0,
                    fee=0,
                    report_path=report_path,
                    status="failed",
                    path=history_database_path,
                )
                progress(f"{warehouse}失败 | 原因：{exc}")
            records.append(record)

    company_summary = build_company_summary(records)
    company_report_path = ensure_unique_report_path(
        period_directory / "企业仓储汇总报告.xlsx"
    )
    company_report_path = build_company_report(
        company_summary,
        [record for record in records if record["status"] == "failed"],
        company_report_path,
    )
    success_count = sum(record["status"] == "success" for record in records)
    failure_count = len(records) - success_count
    finish_run(
        run_id,
        (
            "success"
            if failure_count == 0
            else ("partial" if success_count else "failed")
        ),
        perf_counter() - started,
        history_database_path,
    )
    progress(
        f"批量结束 | 成功{success_count}个 | 失败{failure_count}个"
    )
    return MultiWarehouseResult(
        records, company_summary, company_report_path, run_id
    )
