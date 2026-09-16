from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import pandas as pd


def _decimal(value: Any, field: str) -> Decimal:
    """将输入转换为Decimal。

    输入：任意单元格值和字段名。
    输出：有限Decimal；无效值抛出与旧引擎一致的ValueError。
    业务含义：保证数量和费率使用十进制定点精度。
    FIFO影响：不改变算法，只保持原有数值校验口径。
    """
    if pd.isna(value):
        raise ValueError(f"{field}为空")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}不是有效数字: {value}") from exc
    if not number.is_finite():
        raise ValueError(f"{field}不是有限数字: {value}")
    return number


def _text(value: Any, field: str) -> str:
    """标准化必填文本，不改变业务值及FIFO结果。"""
    if pd.isna(value) or str(value).strip() == "":
        raise ValueError(f"{field}为空")
    return str(value).replace("\u00a0", " ").strip()


def _date(value: Any, field: str) -> pd.Timestamp:
    """解析日期；输入无效时保持旧引擎错误信息和处理方式。"""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{field}缺失或格式无效: {value}")
    return pd.Timestamp(parsed)


def _require_columns(
    frame: pd.DataFrame, required: Iterable[str], table_name: str
) -> None:
    """检查必填列。

    输出：字段完整时无返回值；缺列时抛出ValueError。
    FIFO影响：只在计算前阻止不完整输入，不改变有效数据的FIFO结果。
    """
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"{table_name}缺少字段: {', '.join(missing)}")


def normalize_inputs(
    transactions: pd.DataFrame,
    openings: pd.DataFrame,
    rates: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """标准化三张输入表。

    输入：出入库明细、期初存货、计价规则DataFrame。
    输出：增加源行号并完成文本、日期、Decimal及默认字段处理的副本。
    业务含义：形成FIFO和费率模块使用的稳定字段类型。
    FIFO影响：完全保留旧引擎清洗和排序辅助字段，不改变计算口径。
    """
    tx = transactions.copy()
    op = openings.copy()
    rt = rates.copy()
    tx["源数据行号"] = range(2, len(tx) + 2)
    op["源数据行号"] = range(2, len(op) + 2)
    rt["源数据行号"] = range(2, len(rt) + 2)

    for col in ("仓库名称", "物料名称", "品类", "方向", "批次号"):
        tx[col] = tx[col].map(lambda v: _text(v, col))
    tx["方向"] = tx["方向"].replace({"收货": "入库", "发货": "出库"})
    unsupported = sorted(set(tx["方向"]) - {"入库", "出库"})
    if unsupported:
        raise ValueError(f"出入库明细存在不支持的方向: {unsupported}")
    tx["日期"] = tx["日期"].map(lambda v: _date(v, "日期"))
    tx["数量"] = tx["数量"].map(lambda v: _decimal(v, "数量"))
    if any(value <= 0 for value in tx["数量"]):
        raise ValueError("出入库明细数量必须全部大于0；方向由方向字段决定")

    for col in ("仓库名称", "物料名称", "品类"):
        op[col] = op[col].map(lambda v: _text(v, col))
    if "期初批次号" not in op.columns:
        op["期初批次号"] = [f"期初-{row}" for row in op["源数据行号"]]
    else:
        op["期初批次号"] = op["期初批次号"].map(
            lambda v: "" if pd.isna(v) else str(v).strip()
        )
        op.loc[op["期初批次号"] == "", "期初批次号"] = op.loc[
            op["期初批次号"] == "", "源数据行号"
        ].map(lambda row: f"期初-{row}")
    op["期初数量"] = op["期初数量"].map(lambda v: _decimal(v, "期初数量"))
    if any(value <= 0 for value in op["期初数量"]):
        raise ValueError("期初存货的期初数量必须全部大于0")
    op["期初批次入库基准日期"] = op["期初批次入库基准日期"].map(
        lambda v: _date(v, "期初批次入库基准日期")
    )

    for col in ("仓库名称", "品类或物料", "计费方式说明"):
        rt[col] = rt[col].map(lambda v: _text(v, col))
    rt["单价"] = rt["单价"].map(lambda v: _decimal(v, "单价"))
    if any(value < 0 for value in rt["单价"]):
        raise ValueError("计价规则单价不能小于0")
    if "适用层级" not in rt.columns:
        rt["适用层级"] = "自动"
    rt["适用层级"] = rt["适用层级"].fillna("自动").astype(str).str.strip()
    if "生效日期" not in rt.columns:
        rt["生效日期"] = pd.Timestamp("1900-01-01")
    else:
        rt["生效日期"] = rt["生效日期"].fillna(
            pd.Timestamp("1900-01-01")
        ).map(lambda v: _date(v, "生效日期"))
    return tx, op, rt
