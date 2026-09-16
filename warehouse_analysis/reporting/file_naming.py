from __future__ import annotations

import re
import threading
from pathlib import Path

import pandas as pd


_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_NAME_LOCK = threading.Lock()


def sanitize_filename_component(value: object) -> str:
    """将业务名称转换为可读且可由Windows保存的文件名片段。"""
    text = str(value).strip()
    text = _INVALID_WINDOWS_CHARS.sub("_", text)
    text = text.rstrip(" .")
    return text or "未命名仓库"


def build_report_filename(
    warehouse_name: object,
    start_date: object,
    end_date: object,
    version: object,
) -> str:
    """生成包含仓库、统计期间和版本的标准报告文件名。"""
    warehouse = sanitize_filename_component(warehouse_name)
    start = pd.Timestamp(start_date).strftime("%Y%m%d")
    end = pd.Timestamp(end_date).strftime("%Y%m%d")
    version_text = str(version).strip()
    if version_text.lower().startswith("v"):
        version_text = version_text[1:]
    version_text = sanitize_filename_component(version_text)
    return f"仓储分析报告_{warehouse}_{start}-{end}_V{version_text}.xlsx"


def ensure_unique_report_path(path: str | Path) -> Path:
    """返回不覆盖历史文件的路径，重复时依次追加_001、_002。"""
    target = Path(path)
    with _NAME_LOCK:
        if not target.exists():
            return target
        for index in range(1, 1000):
            candidate = target.with_name(
                f"{target.stem}_{index:03d}{target.suffix}"
            )
            if not candidate.exists():
                return candidate
    raise FileExistsError(f"报告历史版本超过999个，无法生成新文件：{target}")


def resolve_report_output_path(
    output_path: str | Path,
    *,
    warehouse_name: object,
    start_date: object,
    end_date: object,
    version: object,
) -> Path:
    """将目录或自定义文件路径解析为最终不覆盖的报告路径。"""
    target = Path(output_path)
    if target.suffix.lower() != ".xlsx":
        target = target / build_report_filename(
            warehouse_name, start_date, end_date, version
        )
    return ensure_unique_report_path(target)
