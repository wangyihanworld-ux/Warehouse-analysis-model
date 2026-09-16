from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

from warehouse_analysis.rate_manager import DEFAULT_RATE_DATABASE, load_rate_database


def normalize_warehouse_name(value: object) -> str:
    """仅用于诊断比较，不修改ERP原值或费率正式名称。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return re.sub(r"\s+", "", text).casefold()


def _aliases(value: object) -> list[str]:
    return [item.strip() for item in re.split(r"[;；,，\n]", str(value or "")) if item.strip()]


def _match_method(source: str, candidate: str) -> str:
    if source == candidate:
        return "原名称完全匹配"
    if source.strip() == candidate.strip():
        return "去除首尾空格匹配"
    normalized_source = unicodedata.normalize("NFKC", source)
    normalized_candidate = unicodedata.normalize("NFKC", candidate)
    if normalized_source == normalized_candidate:
        if any(char in source + candidate for char in "（）()"):
            return "中文括号/全角半角符号归一化匹配"
        return "全角半角符号归一化匹配"
    if normalize_warehouse_name(source) == normalize_warehouse_name(candidate):
        return "大小写及空格归一化匹配"
    return "名称相似度匹配"


def find_warehouse_rate_candidates(
    warehouse: str,
    database_path: str | Path = DEFAULT_RATE_DATABASE,
    *,
    minimum_similarity: float = 0.85,
) -> list[dict[str, object]]:
    """返回需人工确认的仓库候选；不会选择、替换或保存映射。"""
    source = str(warehouse)
    normalized_source = normalize_warehouse_name(source)
    data = load_rate_database(database_path)
    results: list[dict[str, object]] = []
    for formal_name, group in data.groupby("仓库名称", sort=False):
        formal = str(formal_name)
        comparisons = [(formal, "正式名称")]
        for alias in group.get("仓库别名", pd.Series(dtype=object)):
            comparisons.extend((item, "仓库别名") for item in _aliases(alias))
        best: dict[str, object] | None = None
        for candidate_text, source_kind in comparisons:
            normalized_candidate = normalize_warehouse_name(candidate_text)
            similarity = SequenceMatcher(None, normalized_source, normalized_candidate).ratio()
            if similarity < minimum_similarity:
                continue
            item = {
                "原仓库": source,
                "标准化名称": normalized_source,
                "候选费率": formal,
                "候选比较值": candidate_text,
                "候选来源": source_kind,
                "匹配方式": _match_method(source, candidate_text),
                "相似度": round(similarity * 100),
                "提示": "发现可能匹配规则，请确认。",
            }
            if best is None or int(item["相似度"]) > int(best["相似度"]):
                best = item
        if best is not None and formal != source:
            results.append(best)
    return sorted(results, key=lambda item: (-int(item["相似度"]), str(item["候选费率"])))


def apply_confirmed_warehouse_mapping(
    frame: pd.DataFrame,
    mapping: dict[str, str],
) -> pd.DataFrame:
    """在输入适配层应用用户已确认映射，并保留ERP原仓库值。"""
    if not mapping or "仓库名称" not in frame:
        return frame
    result = frame.copy()
    result.attrs.update(frame.attrs)
    result["ERP仓库原值"] = result["仓库名称"]
    result["仓库名称"] = result["仓库名称"].map(
        lambda value: mapping.get(str(value), value)
    )
    result.attrs["confirmed_warehouse_mapping"] = dict(mapping)
    return result
