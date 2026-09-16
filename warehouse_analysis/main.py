from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

from warehouse_analysis.core.models import StatPeriod
from warehouse_analysis.service import run_analysis


def parse_period(value: str) -> StatPeriod:
    """解析“名称,开始日,截止日”命令行参数，不影响FIFO计算结果。"""
    parts = value.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "统计期间格式必须为 名称,开始日,截止日"
        )
    name, start, end = parts
    start_date = pd.to_datetime(start, errors="coerce")
    end_date = pd.to_datetime(end, errors="coerce")
    if pd.isna(start_date) or pd.isna(end_date) or start_date > end_date:
        raise argparse.ArgumentTypeError(f"无效统计期间: {value}")
    return StatPeriod(
        name=name.strip(),
        start=pd.Timestamp(start_date),
        end=pd.Timestamp(end_date),
    )


def main() -> None:
    """兼容旧命令行参数并通过统一服务层生成业务报告。"""
    parser = argparse.ArgumentParser(
        description="通用FIFO仓储分析与仓储费计算引擎"
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="含出入库明细、期初存货、计价规则三张表的xlsx",
    )
    parser.add_argument(
        "--opening-inventory",
        type=Path,
        help="独立期初库存Excel或四川Oracle HTML伪xls快照",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="输出xlsx"
    )
    parser.add_argument(
        "--rate-config",
        type=Path,
        help="独立费率配置xlsx；省略时兼容读取输入文件内的计价规则Sheet",
    )
    parser.add_argument(
        "--input-type",
        choices=["standard", "separate", "erp", "sichuan_raw"],
        default="standard",
        help="输入类型：standard为历史合并Excel，separate为独立流水和期初，"
        "erp为可配置ERP原始数据，sichuan_raw为四川力庆库历史兼容适配",
    )
    parser.add_argument(
        "--rate-selection",
        help="系统费率库选择项，例如：四川力庆库+统一单价",
    )
    parser.add_argument(
        "--rate-database",
        type=Path,
        default=Path("config") / "rate_database.xlsx",
        help="系统费率库路径",
    )
    parser.add_argument(
        "--period",
        action="append",
        type=parse_period,
        required=True,
        help="可重复指定，格式：名称,开始日,截止日",
    )
    parser.add_argument(
        "--reported-test-percent",
        type=float,
        help="验证用途：按系统金额的指定比例生成上报测试金额，"
        "例如0.05代表+5%%；正式运行请省略",
    )
    args = parser.parse_args()
    if len(args.period) != 1:
        parser.error("桌面工具业务报告当前每次运行只支持一个统计期间")
    service_result = run_analysis(
        args.input,
        args.rate_config,
        args.period[0],
        args.output,
        input_type=args.input_type,
        rate_selection=args.rate_selection,
        rate_database_path=args.rate_database,
        opening_inventory_file=args.opening_inventory,
    )
    if not service_result.success:
        print(
            json.dumps(
                {"运行状态": "失败", "错误": service_result.error},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(
        json.dumps(
            {
                "运行状态": "成功",
                "输出文件": str(service_result.output_path),
                **service_result.summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
