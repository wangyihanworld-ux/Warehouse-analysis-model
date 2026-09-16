from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class StatPeriod:
    """统计期间模型。

    输入：期间名称、开始时间和截止时间。
    输出：供计算引擎使用的不可变统计期间对象。
    业务含义：界定报表展示期间和统计截止时点。
    FIFO影响：仅承载参数，不改变FIFO顺序或计算结果。
    """

    name: str
    start: pd.Timestamp
    end: pd.Timestamp


@dataclass
class EngineResult:
    """计算引擎结果集合。

    输入：run_engine生成的七张DataFrame结果。
    输出：供报告、测试和其他系统调用的结构化结果。
    业务含义：集中承载批次、汇总、月末快照、守恒和异常数据。
    FIFO影响：仅承载结果，不改变FIFO顺序或计算结果。
    """

    batch_details: pd.DataFrame
    material_summary: pd.DataFrame
    category_summary: pd.DataFrame
    warehouse_total: pd.DataFrame
    monthly_snapshot: pd.DataFrame
    conservation: pd.DataFrame
    anomalies: pd.DataFrame
