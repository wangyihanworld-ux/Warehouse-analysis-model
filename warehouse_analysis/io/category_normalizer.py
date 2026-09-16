from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd


def standardize_category_parts(values: Iterable[Any]) -> str:
    """按ERP既有规则拼接非空品类层级，形成唯一标准品类字符串。"""
    parts: list[str] = []
    for value in values:
        if value is None or pd.isna(value):
            continue
        text = str(value).replace("\u00a0", " ").strip()
        if text and text not in parts:
            parts.append(text)
    return "/".join(parts)
