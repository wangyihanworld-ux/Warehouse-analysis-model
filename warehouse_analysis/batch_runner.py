from __future__ import annotations

import json
import sys
import threading
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Callable, Mapping

import pandas as pd

from warehouse_analysis.logging_config import configure_logging
from warehouse_analysis.rate_manager import DEFAULT_RATE_DATABASE
from warehouse_analysis.reporting.file_naming import resolve_report_output_path
from warehouse_analysis.service import AnalysisRunResult, run_analysis
from warehouse_analysis.version import VERSION


_HISTORY_LOCK = threading.Lock()


def default_history_path() -> Path:
    """返回源码或EXE发布目录下的历史运行记录路径。"""
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return root / "config" / "run_history.json"


@dataclass(frozen=True)
class MonthPeriod:
    """单个月度任务的标准期间。"""

    month: str
    start: pd.Timestamp
    end: pd.Timestamp


@dataclass(frozen=True)
class BatchAnalysisResult:
    """批量任务总体结果；单月失败不会中断后续月份。"""

    records: list[dict[str, object]]

    @property
    def success_count(self) -> int:
        return sum(record["status"] == "success" for record in self.records)

    @property
    def failure_count(self) -> int:
        return sum(record["status"] == "failed" for record in self.records)


def generate_month_periods(start_month: str, end_month: str) -> list[MonthPeriod]:
    """按YYYY-MM生成闭区间内每个月首日至月末。"""
    try:
        start = datetime.strptime(start_month.strip(), "%Y-%m")
        end = datetime.strptime(end_month.strip(), "%Y-%m")
    except ValueError as exc:
        raise ValueError("月份格式必须为YYYY-MM，例如2026-01。") from exc
    if start > end:
        raise ValueError("开始月份不能晚于结束月份。")
    current = pd.Timestamp(start)
    last = pd.Timestamp(end)
    periods: list[MonthPeriod] = []
    while current <= last:
        last_day = monthrange(current.year, current.month)[1]
        periods.append(
            MonthPeriod(
                current.strftime("%Y-%m"),
                pd.Timestamp(current.year, current.month, 1),
                pd.Timestamp(current.year, current.month, last_day),
            )
        )
        current = current + pd.offsets.MonthBegin(1)
    return periods


def load_run_history(path: str | Path | None = None) -> list[dict[str, object]]:
    """读取历史记录；文件不存在或损坏时返回空列表。"""
    target = Path(path) if path is not None else default_history_path()
    if not target.is_file():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("runs", [])
    return payload if isinstance(payload, list) else []


def append_run_history(
    record: Mapping[str, object],
    path: str | Path | None = None,
) -> Path:
    """线程安全、原子追加一条运行历史。"""
    target = Path(path) if path is not None else default_history_path()
    with _HISTORY_LOCK:
        history = load_run_history(target)
        history.append(dict(record))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"runs": history}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
    return target


def build_history_record(
    *,
    input_file: str | Path,
    opening_inventory_file: str | Path | None,
    rate_selection: str | None,
    period: str,
    period_start: object,
    period_end: object,
    output_path: str | Path,
    result: AnalysisRunResult,
    elapsed_seconds: float,
) -> dict[str, object]:
    """将服务返回值转换为可持久化的历史记录。"""
    warehouse = (
        rate_selection.split("+", 1)[0]
        if rate_selection and "+" in rate_selection
        else ""
    )
    return {
        "run_time": datetime.now().isoformat(timespec="seconds"),
        "version": VERSION,
        "input_file": str(Path(input_file)),
        "opening_inventory_file": (
            str(Path(opening_inventory_file))
            if opening_inventory_file
            else ""
        ),
        "warehouse": warehouse,
        "rate": rate_selection or "",
        "period": period,
        "period_start": pd.Timestamp(period_start).strftime("%Y-%m-%d"),
        "period_end": pd.Timestamp(period_end).strftime("%Y-%m-%d"),
        "output_path": str(Path(output_path)),
        "report_filename": Path(output_path).name,
        "status": "success" if result.success else "failed",
        "fee": (
            float(result.summary.get("仓储费总额（元）", 0))
            if result.success
            else None
        ),
        "elapsed_seconds": round(float(elapsed_seconds), 3),
        "error": result.error or "",
    }


def run_batch_analysis(
    input_file: str | Path,
    opening_inventory_file: str | Path | None,
    rate_selection: str | None,
    start_month: str,
    end_month: str,
    output_directory: str | Path,
    *,
    rate_config: str | Path | None = None,
    input_type: str = "separate",
    rate_database_path: str | Path = DEFAULT_RATE_DATABASE,
    erp_transaction_mapping: Mapping[str, str] | None = None,
    erp_opening_mapping: Mapping[str, str] | None = None,
    history_path: str | Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> BatchAnalysisResult:
    """逐月调用service.run_analysis生成报告，不包含任何计算逻辑。"""
    logger = configure_logging()
    periods = generate_month_periods(start_month, end_month)
    output_root = Path(output_directory) / "reports"
    records: list[dict[str, object]] = []

    def progress(message: str) -> None:
        logger.info(message)
        if progress_callback:
            progress_callback(message)

    progress(
        f"批量任务开始 | 月份={start_month}至{end_month} | "
        f"任务数={len(periods)}"
    )
    for period in periods:
        started = perf_counter()
        month_directory = output_root / period.month
        warehouse_name = (
            rate_selection.split("+", 1)[0]
            if rate_selection and "+" in rate_selection
            else "未命名仓库"
        )
        output_path = resolve_report_output_path(
            month_directory,
            warehouse_name=warehouse_name,
            start_date=period.start,
            end_date=period.end,
            version=VERSION,
        )
        progress(f"正在生成：{period.start.strftime('%Y年%m月')}报告")
        try:
            result = run_analysis(
                input_file,
                rate_config,
                {
                    "name": period.month,
                    "start": period.start,
                    "end": period.end,
                },
                month_directory,
                input_type=input_type,
                rate_selection=rate_selection,
                rate_database_path=rate_database_path,
                opening_inventory_file=opening_inventory_file,
                erp_transaction_mapping=erp_transaction_mapping,
                erp_opening_mapping=erp_opening_mapping,
                erp_opening_baseline_date=periods[0].start,
            )
        except Exception as exc:  # 服务正常会返回失败；保留调度级隔离
            result = AnalysisRunResult(
                False, None, {}, f"批量调度捕获异常：{exc}"
            )
        elapsed = perf_counter() - started
        actual_output_path = result.output_path or output_path
        record = build_history_record(
            input_file=input_file,
            opening_inventory_file=opening_inventory_file,
            rate_selection=rate_selection,
            period=period.month,
            period_start=period.start,
            period_end=period.end,
            output_path=actual_output_path,
            result=result,
            elapsed_seconds=elapsed,
        )
        append_run_history(record, history_path)
        records.append(record)
        if result.success:
            progress(
                f"完成：{period.start.strftime('%Y年%m月')}报告 | "
                f"输出={actual_output_path} | 耗时={elapsed:.3f}秒"
            )
        else:
            progress(
                f"失败：{period.start.strftime('%Y年%m月')}报告 | "
                f"原因={result.error} | 耗时={elapsed:.3f}秒；继续下一月份"
            )
    progress(
        f"批量任务结束 | 成功={sum(r['status'] == 'success' for r in records)} | "
        f"失败={sum(r['status'] == 'failed' for r in records)}"
    )
    return BatchAnalysisResult(records)
