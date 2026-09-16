from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from time import perf_counter
from typing import Callable, Mapping, Sequence

import pandas as pd

from warehouse_analysis.analysis.sensitivity import (
    build_fee_sensitivity,
    build_sensitivity_ranking,
)
from warehouse_analysis.analysis.summary import build_warehouse_summary
from warehouse_analysis.core.calculation import run_engine
from warehouse_analysis.core.models import StatPeriod
from warehouse_analysis.core.rate_engine import RateResolver
from warehouse_analysis.core.validation import normalize_inputs
from warehouse_analysis.io.excel_loader import (
    InputWorkbookError,
    load_analysis_inputs,
    load_business_workbook,
    load_rate_config,
    load_separate_business_files,
    load_transaction_file,
)
from warehouse_analysis.io.source_adapters import load_sichuan_raw_data
from warehouse_analysis.io.erp_adapter import load_erp_inputs
from warehouse_analysis.io.quantity_normalizer import (
    normalize_transaction_quantity,
)
from warehouse_analysis.io.opening_quantity_normalizer import (
    OpeningFieldAmbiguityError,
    OpeningInventoryQuantityError,
    normalize_opening_inventory_quantity,
)
from warehouse_analysis.io.opening_date_normalizer import (
    filter_transactions_before_start,
    normalize_opening_inventory_dates,
)
from warehouse_analysis.logging_config import configure_logging
from warehouse_analysis.io.input_check_report import build_input_check_report
from warehouse_analysis.io.input_validator import (
    InputValidationError,
    validate_input_files,
    validate_standardized_inputs,
)
from warehouse_analysis.rate_manager import (
    DEFAULT_RATE_DATABASE,
    RateRuleCompatibilityError,
    get_effective_rate_rule,
    rate_match_diagnosis,
)
from warehouse_analysis.rate_match_diagnosis import (
    apply_confirmed_warehouse_mapping,
    find_warehouse_rate_candidates,
    normalize_warehouse_name,
)
from warehouse_analysis.reporting.excel_report import build_business_report
from warehouse_analysis.version import VERSION


@dataclass(frozen=True)
class AnalysisRunResult:
    """桌面工具与CLI共用的运行结果。"""

    success: bool
    output_path: Path | None
    summary: dict[str, object]
    error: str | None = None


PeriodLike = (
    StatPeriod
    | Mapping[str, object]
    | Sequence[object]
)


def _strict_date(value: object, field: str) -> pd.Timestamp:
    """按YYYY-MM-DD严格解析业务人员输入的日期。"""
    if isinstance(value, pd.Timestamp):
        return value
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"{field}格式错误：{value}\n建议：请按YYYY-MM-DD填写，例如2026-03-31。"
        ) from exc
    return pd.Timestamp(parsed)


def _coerce_period(period: PeriodLike) -> StatPeriod:
    """将GUI/CLI期间参数转换为核心层StatPeriod，不改变计算口径。"""
    if isinstance(period, StatPeriod):
        result = period
    elif isinstance(period, Mapping):
        start = _strict_date(period.get("start"), "统计开始日期")
        end = _strict_date(period.get("end"), "统计结束日期")
        name = str(period.get("name") or f"{start.date()}至{end.date()}")
        result = StatPeriod(name=name, start=start, end=end)
    elif isinstance(period, Sequence) and not isinstance(period, (str, bytes)):
        if len(period) not in {2, 3}:
            raise ValueError("统计期间必须包含开始日期、结束日期和可选期间名称。")
        start = _strict_date(period[0], "统计开始日期")
        end = _strict_date(period[1], "统计结束日期")
        name = str(period[2]) if len(period) == 3 else f"{start.date()}至{end.date()}"
        result = StatPeriod(name=name, start=start, end=end)
    else:
        raise ValueError("统计期间参数无效，请填写开始日期和结束日期。")
    if result.start > result.end:
        raise ValueError("统计开始日期不能晚于统计结束日期。")
    return result


def _build_rate_match_result(
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
    rates: pd.DataFrame,
    batch_details: pd.DataFrame,
    rate_context: Mapping[str, object] | None = None,
) -> pd.DataFrame:
    """为报告生成费率追溯记录，不改变费率匹配或费用计算结果。"""
    tx, op, normalized_rates = normalize_inputs(
        transactions, openings, rates
    )
    resolver = RateResolver(
        normalized_rates,
        set(tx["物料名称"]) | set(op["物料名称"]),
        set(tx["品类"]) | set(op["品类"]),
    )
    rows: list[dict[str, object]] = []
    for _, detail in batch_details.iterrows():
        effective_date = (
            detail["出库日期"]
            if pd.notna(detail["出库日期"])
            else detail["统计截止日"]
        )
        match = resolver.resolve(
            str(detail["仓库名称"]),
            str(detail["物料名称"]),
            str(detail["品类"]),
            pd.Timestamp(effective_date),
        )
        source = normalized_rates[
            normalized_rates["源数据行号"] == match.source_row
        ].iloc[0]
        auto_value = source.get("是否自动计算", "是")
        automatic = str(auto_value).strip().lower() not in {
            "否",
            "no",
            "false",
            "0",
            "n",
        }
        special_rule = source.get("特殊规则类型", "")
        rows.append(
            {
                "仓库": detail["仓库名称"],
                "统计期间": detail["统计期间"],
                "统计开始日期": detail["统计开始日"],
                "统计截止日期": detail["统计截止日"],
                "物料/品类": detail["物料名称"],
                "品类": detail["品类"],
                "匹配层级": match.level,
                "匹配对象": match.target,
                "单价": float(match.rate),
                "生效日期": match.effective_date,
                "计费方式说明": match.note,
                "配置源行号": match.source_row,
                "是否人工核对": "否" if automatic else "是",
                "特殊规则类型": (
                    "" if pd.isna(special_rule) else str(special_rule)
                ),
                "人工核对原因": (
                    ""
                    if automatic
                    else (
                        str(special_rule).strip()
                        or "该费率规则配置为不自动计算"
                    )
                ),
                "费率库来源": (
                    str(rate_context.get("费率库来源", "外部费率配置"))
                    if rate_context
                    else "外部费率配置"
                ),
                "计费规则": (
                    str(rate_context.get("计费规则", match.note))
                    if rate_context
                    else match.note
                ),
                "规则版本": (
                    rate_context.get("规则版本", match.effective_date)
                    if rate_context
                    else match.effective_date
                ),
            }
        )
    columns = [
        "仓库",
        "统计期间",
        "统计开始日期",
        "统计截止日期",
        "物料/品类",
        "品类",
        "匹配层级",
        "匹配对象",
        "单价",
        "生效日期",
        "计费方式说明",
        "配置源行号",
        "是否人工核对",
        "特殊规则类型",
        "人工核对原因",
        "费率库来源",
        "计费规则",
        "规则版本",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    return (
        pd.DataFrame(rows, columns=columns)
        .drop_duplicates()
        .sort_values(
            ["仓库", "统计期间", "物料/品类", "生效日期", "配置源行号"],
            kind="stable",
        )
        .reset_index(drop=True)
    )


def _friendly_error(
    exc: Exception,
    rate_diagnosis: Mapping[str, object] | None = None,
) -> str:
    """将底层异常转换为GUI和CLI可直接展示的业务提示。"""
    if isinstance(
        exc,
        (
            InputWorkbookError,
            OpeningFieldAmbiguityError,
            OpeningInventoryQuantityError,
            InputValidationError,
        ),
    ):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return (
            f"问题：文件不存在：{exc.filename or exc}\n"
            "位置：输入文件\n建议：请重新选择有效文件。"
        )
    message = str(exc)
    if isinstance(exc, RateRuleCompatibilityError):
        diagnosis = exc.diagnosis
        return (
            "问题：\n费率规则需要业务确认\n\n"
            "位置：\n费率规则诊断 / FIFO执行前\n\n"
            "发现：\n"
            f"仓库：{diagnosis.get('仓库', '')}\n"
            f"规则名称：{diagnosis.get('规则名称', '')}\n"
            f"规则层级：{diagnosis.get('规则层级', '')}\n"
            f"当前规则类型：{diagnosis.get('当前规则类型', '')}\n"
            f"状态：{diagnosis.get('状态', '')}\n\n"
            f"原因：\n{diagnosis.get('为什么无法自动计算', '')}\n\n"
            f"建议：\n{diagnosis.get('建议处理方式', '')}"
        )
    if "没有匹配的计价规则" in message:
        if rate_diagnosis:
            search = rate_diagnosis.get("当前搜索", {})
            return (
                "问题：\n没有匹配的计价规则\n\n"
                "位置：\n费率匹配 / FIFO执行前\n\n"
                "发现：\n"
                f"仓库：{rate_diagnosis.get('仓库名称')}\n"
                f"物料：{rate_diagnosis.get('物料名称')}\n"
                f"品类：{rate_diagnosis.get('品类')}\n"
                f"日期：{rate_diagnosis.get('统计日期')}\n\n"
                "当前搜索：\n"
                f"物料级：{search.get('物料级', '无')}\n"
                f"品类级：{search.get('品类级', '无')}\n"
                f"仓库级：{search.get('仓库级', '无')}\n\n"
                "建议：\n1. 新增该仓库费率\n"
                "2. 检查仓库名称是否一致\n3. 检查生效日期"
            )
        return (
            f"问题：{message}\n位置：计价规则\n"
            "建议：请补充该仓库对应的物料、品类或全部层级费率。"
        )
    return (
        f"问题：{message or exc.__class__.__name__}\n"
        "位置：分析流程\n建议：请根据提示检查输入文件、日期和费率配置。"
    )


def _write_rate_diagnosis_report(
    output_path: Path,
    diagnosis: Mapping[str, object],
) -> Path:
    """生成仅含费率失败异常的诊断工作簿，不调用任何计算模块。"""
    target = output_path.with_name(f"{output_path.stem}_费率诊断.xlsx")
    counter = 1
    while target.exists():
        target = output_path.with_name(
            f"{output_path.stem}_费率诊断_{counter:03d}.xlsx"
        )
        counter += 1
    search = diagnosis.get("当前搜索", {})
    is_rule_conflict = diagnosis.get("诊断类型") == "费率规则类型冲突"
    row = {
        "问题": "费率规则类型冲突" if is_rule_conflict else "没有匹配的计价规则",
        "位置": "费率规则诊断 / FIFO执行前" if is_rule_conflict else "费率匹配 / FIFO执行前",
        "仓库": diagnosis.get("仓库", diagnosis.get("仓库名称", "")),
        "物料": diagnosis.get("物料名称", ""),
        "品类": diagnosis.get("品类", ""),
        "日期": diagnosis.get("统计日期", ""),
        "物料级搜索": search.get("物料级", "无"),
        "品类级搜索": search.get("品类级", "无"),
        "仓库级搜索": search.get("仓库级", "无"),
        "规则名称": diagnosis.get("规则名称", ""),
        "规则层级": diagnosis.get("规则层级", ""),
        "当前规则类型": diagnosis.get("当前规则类型", ""),
        "状态": diagnosis.get("状态", ""),
        "失败原因": diagnosis.get("失败原因", diagnosis.get("为什么无法自动计算", "")),
        "建议": diagnosis.get("建议动作", diagnosis.get("建议处理方式", "")),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    candidates = list(diagnosis.get("候选列表", []))
    searches = diagnosis.get("当前搜索", {})
    search_rows = [
        {
            "仓库原值": diagnosis.get("仓库名称", ""),
            "标准化名称": normalize_warehouse_name(diagnosis.get("仓库名称", "")),
            "搜索层级": level,
            "搜索结果": value,
            "最终结果": diagnosis.get("失败原因", ""),
        }
        for level, value in searches.items()
    ]
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        pd.DataFrame([row]).to_excel(writer, sheet_name="异常记录", index=False)
        pd.DataFrame(
            candidates,
            columns=[
                "原仓库", "标准化名称", "候选费率", "候选比较值",
                "候选来源", "匹配方式", "相似度", "提示",
            ],
        ).to_excel(writer, sheet_name="候选匹配", index=False)
        pd.DataFrame(search_rows).to_excel(
            writer, sheet_name="费率搜索过程", index=False
        )
    return target


def run_analysis(
    input_file: str | Path,
    rate_config: str | Path | None,
    period: PeriodLike,
    output_path: str | Path,
    progress_callback: Callable[[str], None] | None = None,
    input_type: str = "standard",
    rate_selection: str | None = None,
    rate_database_path: str | Path = DEFAULT_RATE_DATABASE,
    opening_inventory_file: str | Path | None = None,
    erp_transaction_mapping: Mapping[str, str] | None = None,
    erp_opening_mapping: Mapping[str, str] | None = None,
    erp_opening_baseline_date: object | None = None,
    opening_field_mapping: Mapping[str, str] | None = None,
    warehouse_name_mapping: Mapping[str, str] | None = None,
) -> AnalysisRunResult:
    """运行仓储分析全流程并生成最终业务报告。

    本服务只协调已有输入、计算、经营分析、敏感性和报告模块，不实现
    FIFO、费率公式、Excel解析或分析口径。
    """
    started = perf_counter()
    logger = configure_logging()
    input_check_report = pd.DataFrame()
    rate_diagnosis_result: dict[str, object] = {}

    def progress(message: str) -> None:
        logger.info(message)
        if progress_callback is not None:
            progress_callback(message)

    try:
        progress(f"开始运行 | 模型版本={VERSION}")
        business_path = Path(input_file)
        opening_path = (
            Path(opening_inventory_file)
            if opening_inventory_file
            else None
        )
        rate_path = Path(rate_config) if rate_config else None
        validate_input_files(business_path, opening_path)
        if opening_path is not None and not opening_path.is_file():
            raise FileNotFoundError(str(opening_path))
        if rate_path is not None and not rate_path.is_file():
            raise FileNotFoundError(str(rate_path))
        stat_period = _coerce_period(period)
        destination = Path(output_path)

        logger.info(
            "运行参数 | 输入流水文件=%s | 期初库存文件=%s | 费率文件=%s | "
            "费率规则=%s | 统计期间=%s至%s | 输出文件=%s",
            business_path,
            opening_path or "历史三表合一文件内期初Sheet",
            rate_path or "业务文件内计价规则Sheet",
            rate_selection or "外部/文件内费率",
            stat_period.start.date(),
            stat_period.end.date(),
            destination,
        )
        progress("正在读取输入文件...")
        progress("读取期初库存...")
        rate_context: dict[str, object] = {}
        if input_type == "standard":
            if opening_path is not None:
                transactions, openings = load_separate_business_files(
                    business_path,
                    opening_path,
                    default_inventory_date=stat_period.start,
                    opening_field_mapping=opening_field_mapping,
                )
                if not rate_selection:
                    if rate_path is None:
                        raise ValueError(
                            "独立文件模式必须选择系统费率规则或提供费率配置文件"
                        )
                    rates = load_rate_config(rate_path)
            elif rate_selection:
                transactions, openings = load_business_workbook(
                    business_path
                )
            else:
                transactions, openings, rates = load_analysis_inputs(
                    business_path, rate_path
                )
        elif input_type == "separate":
            if opening_path is None:
                transactions = load_transaction_file(business_path)
                start = stat_period.start
                end = stat_period.end
                dates = pd.to_datetime(transactions["日期"], errors="coerce")
                in_period = (dates >= start) & (dates <= end)
                inbound = transactions[
                    in_period & (transactions["方向"].astype(str).str.strip() == "入库")
                ]["数量"]
                outbound = transactions[
                    in_period & (transactions["方向"].astype(str).str.strip() == "出库")
                ]["数量"]
                raise ValueError(
                    "缺少期初库存文件，无法计算FIFO库存及仓储费用，请上传期初库存。"
                    f"\n期间入库量：{pd.to_numeric(inbound, errors='coerce').sum():.3f}吨"
                    f"\n期间出库量：{pd.to_numeric(outbound, errors='coerce').sum():.3f}吨"
                )
            transactions, openings = load_separate_business_files(
                business_path,
                opening_path,
                default_inventory_date=stat_period.start,
                opening_field_mapping=opening_field_mapping,
            )
            if not rate_selection:
                if rate_path is None:
                    raise ValueError(
                        "独立文件模式必须选择系统费率规则或提供费率配置文件"
                    )
                rates = load_rate_config(rate_path)
        elif input_type == "sichuan_raw":
            transactions, openings = load_sichuan_raw_data(
                business_path, opening_path
            )
            if not rate_selection and rate_path is None:
                raise ValueError(
                    "四川力庆库原始数据模式必须选择系统费率规则或独立费率配置文件"
                )
        elif input_type == "erp":
            if opening_path is None:
                raise ValueError(
                    "缺少ERP期初库存文件，无法计算FIFO库存及仓储费用，请上传期初库存。"
                )
            progress("ERP识别开始。")
            (
                transactions,
                openings,
                transaction_detection,
                opening_detection,
            ) = load_erp_inputs(
                business_path,
                opening_path,
                transaction_mapping=erp_transaction_mapping,
                opening_mapping=erp_opening_mapping,
                period_start=(
                    erp_opening_baseline_date
                    if erp_opening_baseline_date is not None
                    else stat_period.start
                ),
            )
            logger.info(
                "ERP流水识别 | 文件=%s | Sheet=%s | 字段映射=%s",
                business_path,
                transaction_detection.sheet_name,
                transactions.attrs.get("erp_mapping", {}),
            )
            logger.info(
                "ERP期初识别 | 文件=%s | Sheet=%s | 字段映射=%s",
                opening_path,
                opening_detection.sheet_name,
                erp_opening_mapping or opening_detection.resolved_mapping,
            )
            progress("ERP字段转换完成。")
            if not rate_selection and rate_path is None:
                raise ValueError(
                    "ERP原始数据模式必须选择系统费率规则或独立费率配置文件"
                )
        else:
            raise ValueError(
                f"不支持的输入类型：{input_type}；"
                "请选择standard、separate、erp或sichuan_raw"
            )
        transactions = normalize_transaction_quantity(transactions)
        quantity_info = transactions.attrs.get("quantity_normalization", {})
        quantity_mode = quantity_info.get("mode", "数量正数+方向模式")
        progress(f"检测到数量格式：{quantity_mode}")
        logger.info(
            "数量标准化 | 数量模式=%s | 转换后入库=%s吨 | "
            "转换后出库=%s吨 | 零数量异常=%s条",
            quantity_mode,
            quantity_info.get("inbound_total", 0),
            quantity_info.get("outbound_total", 0),
            quantity_info.get("zero_count", 0),
        )
        input_quantity_anomalies = list(
            transactions.attrs.get("quantity_anomalies", [])
        )
        openings = normalize_opening_inventory_quantity(
            openings,
            file_path=opening_path or business_path,
            sheet_name=openings.attrs.get("source_sheet"),
        )
        opening_check = openings.attrs.get("opening_inventory_check", {})
        progress(
            f"期初库存数量模式：{opening_check.get('数量模式', '正常模式')}"
        )
        logger.info(
            "期初库存检查 | 数量模式=%s | 总记录=%s | 有效=%s | "
            "过滤=%s | 零库存=%s | 负库存=%s | 最终进入FIFO=%s",
            opening_check.get("数量模式", "正常模式"),
            opening_check.get("总记录数", len(openings)),
            opening_check.get("有效库存记录", len(openings)),
            opening_check.get("过滤汇总记录", 0),
            opening_check.get("零数量记录", 0),
            opening_check.get("负库存记录", 0),
            opening_check.get("最终进入FIFO", len(openings)),
        )
        openings = normalize_opening_inventory_dates(
            openings,
            stat_period.start,
        )
        opening_date_type = openings.attrs.get("opening_date_type", "REAL")
        progress(f"期初日期类型：{opening_date_type}")
        logger.info(
            "期初库存日期 | 日期类型=%s | 期初基准日期=%s | 来源=%s",
            opening_date_type,
            stat_period.start.date(),
            openings.attrs.get("opening_inventory_source", ""),
        )
        progress("正在执行FIFO前输入预检查...")
        validate_standardized_inputs(transactions, openings)
        progress("输入预检查通过。")
        if warehouse_name_mapping:
            transactions = apply_confirmed_warehouse_mapping(
                transactions, dict(warehouse_name_mapping)
            )
            openings = apply_confirmed_warehouse_mapping(
                openings, dict(warehouse_name_mapping)
            )
            logger.info(
                "用户确认仓库名称映射 | 映射=%s", dict(warehouse_name_mapping)
            )
        input_check_report = build_input_check_report(transactions, openings)
        for row in input_check_report.to_dict("records"):
            check_message = (
                f"输入检查 | 类型={row['输入类型']} | 文件={row['文件名称']} | "
                f"Sheet={row['Sheet名称']} | 识别字段={row['识别字段']} | "
                f"数量模式={row['数量模式']} | 日期类型={row['期初日期类型']} | "
                f"缺失字段={row['缺失字段'] or '无'} | 建议={row['建议']}"
            )
            progress(check_message)
        transactions, history_check = filter_transactions_before_start(
            transactions,
            stat_period.start,
        )
        historical_count = int(history_check["历史流水记录数"])
        progress(
            f"发现统计开始日前流水：{historical_count}条，已排除。"
        )
        logger.info(
            "历史流水过滤 | 统计开始日期=%s | 发现=%s条 | 数量=%s吨 | "
            "金额=%s | 原始记录=%s | 进入FIFO=%s | 已排除",
            stat_period.start.date(),
            historical_count,
            history_check["历史流水数量"],
            (
                history_check["历史流水金额"]
                if history_check["历史流水金额"] is not None
                else "源数据无金额字段"
            ),
            history_check["原始流水记录数"],
            history_check["进入FIFO流水记录数"],
        )
        progress("正在读取费率配置...")
        if rate_selection:
            rates, rule_version = get_effective_rate_rule(
                rate_selection,
                stat_period.end,
                rate_database_path,
            )
            rate_context = {
                "费率库来源": Path(rate_database_path).name,
                "计费规则": rate_selection.split("+", 1)[-1],
                "规则版本": rule_version,
            }
            prices = sorted(set(map(float, rates["单价"])))
            logger.info(
                "使用费率 | 选择项=%s | 费率版本日期=%s | 单价=%s",
                rate_selection,
                rule_version.date(),
                ",".join(f"{price:.4f}" for price in prices),
            )
        elif input_type in {"sichuan_raw", "erp"}:
            rates = load_rate_config(rate_path)
            rate_context = {
                "费率库来源": Path(rate_path).name,
                "计费规则": "外部费率配置",
                "规则版本": pd.to_datetime(
                    rates.get("生效日期"), errors="coerce"
                ).max(),
            }
        else:
            rate_context = {
                "费率库来源": (
                    Path(rate_path).name if rate_path else business_path.name
                ),
                "计费规则": "外部费率配置",
                "规则版本": pd.to_datetime(
                    rates.get("生效日期"), errors="coerce"
                ).max(),
            }
        progress("正在执行FIFO计算...")
        result = run_engine(
            transactions, openings, rates, [stat_period]
        )
        if input_quantity_anomalies:
            result.anomalies = pd.concat(
                [
                    result.anomalies,
                    pd.DataFrame(input_quantity_anomalies),
                ],
                ignore_index=True,
                sort=False,
            )
        progress("FIFO计算完成。")
        progress("正在生成仓库经营分析...")
        warehouse_summary = build_warehouse_summary(
            result,
            transactions,
            openings,
            rates,
            [stat_period],
        )
        progress("仓库经营分析完成。")
        progress("正在生成敏感性分析...")
        fee_sensitivity = build_fee_sensitivity(warehouse_summary)
        sensitivity_ranking = build_sensitivity_ranking(fee_sensitivity)
        progress("敏感性分析完成。")
        rate_matches = _build_rate_match_result(
            transactions,
            openings,
            rates,
            result.batch_details,
            rate_context,
        )
        progress("正在生成Excel报告...")
        destination = build_business_report(
            result,
            warehouse_summary,
            fee_sensitivity,
            sensitivity_ranking,
            pd.DataFrame(),
            rate_matches,
            destination,
            report_context={
                "期初库存来源": openings.attrs.get(
                    "opening_inventory_source", ""
                ),
                "日期口径": openings.attrs.get(
                    "opening_date_description", ""
                ),
            },
        )
        progress("Excel报告生成完成。")
        total_fee = float(warehouse_summary["仓储费总额（元）"].sum())
        ending_inventory = float(
            warehouse_summary["期末库存量（吨）"].sum()
        )
        unbalanced = int(
            (result.conservation["校验结果"] != "平衡").sum()
        )
        manual_count = int(
            rate_matches["是否人工核对"].map(
                lambda value: str(value).strip() == "是"
            ).sum()
        )
        summary = {
            "仓库数量": int(warehouse_summary["仓库名称"].nunique()),
            "统计期间": stat_period.name,
            "批次明细行数": len(result.batch_details),
            "期末库存量（吨）": ending_inventory,
            "仓储费总额（元）": total_fee,
            "异常记录数": len(result.anomalies),
            "人工核对数量": manual_count,
            "数量守恒不平批次": unbalanced,
            "运行耗时（秒）": perf_counter() - started,
        }
        if len(warehouse_summary) == 1:
            warehouse_row = warehouse_summary.iloc[0]
            summary.update(
                {
                    "仓库名称": warehouse_row["仓库名称"],
                    "平均库存量（吨）": float(
                        warehouse_row["平均库存量（吨）"]
                    ),
                    "平均存放天数": float(
                        warehouse_row["平均存放天数"]
                    ),
                    "平均仓储费率（元/吨/天）": float(
                        warehouse_row["平均仓储费率（元/吨/天）"]
                    ),
                }
            )
        summary["输入检查报告"] = input_check_report.to_dict("records")
        elapsed = perf_counter() - started
        logger.info(
            "运行结束 | 状态=成功 | 输出文件=%s | 运行耗时=%.3f秒",
            destination,
            elapsed,
        )
        if progress_callback is not None:
            progress_callback("完成。")
        return AnalysisRunResult(True, destination, summary)
    except Exception as exc:
        if isinstance(exc, OpeningInventoryQuantityError):
            check = exc.check
            logger.error(
                "期初库存检查 | 数量模式=%s | 总记录=%s | 有效=%s | "
                "过滤=%s | 零库存=%s | 负库存=%s | 最终进入FIFO=0",
                check.get("数量模式"),
                check.get("总记录数"),
                check.get("有效库存记录"),
                check.get("过滤汇总记录"),
                check.get("零数量记录"),
                check.get("负库存记录"),
            )
        message = str(exc)
        if isinstance(exc, RateRuleCompatibilityError):
            rate_diagnosis_result = dict(exc.diagnosis)
        match = re.search(
            r"没有匹配的计价规则:\s*仓库=(.*?),\s*物料=(.*?),\s*"
            r"品类=(.*?),\s*日期=(\d{4}-\d{2}-\d{2})",
            message,
        )
        if match and not rate_diagnosis_result:
            try:
                rate_diagnosis_result = rate_match_diagnosis(
                    match.group(1),
                    match.group(2),
                    match.group(3),
                    match.group(4),
                    rate_database_path,
                )
                rate_diagnosis_result["候选列表"] = find_warehouse_rate_candidates(
                    match.group(1), rate_database_path
                )
            except Exception as diagnosis_exc:
                logger.warning("费率诊断失败：%s", diagnosis_exc)
        diagnosis_report = None
        if rate_diagnosis_result:
            try:
                diagnosis_report = _write_rate_diagnosis_report(
                    destination, rate_diagnosis_result
                )
                logger.error("费率诊断报告 | 异常记录Sheet=%s", diagnosis_report)
            except Exception as report_exc:
                logger.warning("费率诊断报告生成失败：%s", report_exc)
        friendly = _friendly_error(exc, rate_diagnosis_result)
        logger.error(
            "运行失败 | 错误类型=%s | 错误信息=%s | 业务提示=%s | 运行耗时=%.3f秒",
            exc.__class__.__name__,
            str(exc),
            friendly.replace("\n", " | "),
            perf_counter() - started,
            exc_info=True,
        )
        if progress_callback is not None:
            progress_callback(f"运行失败：{friendly}")
        return AnalysisRunResult(
            success=False,
            output_path=diagnosis_report,
            summary=(
                {
                    "费率诊断": rate_diagnosis_result,
                    "费率诊断报告": str(diagnosis_report) if diagnosis_report else "",
                }
                if rate_diagnosis_result
                else {}
            ),
            error=friendly,
        )
