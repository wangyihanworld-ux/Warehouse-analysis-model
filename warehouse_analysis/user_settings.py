from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Mapping


DEFAULT_SETTINGS = {
    "last_input_file": "",
    "last_opening_inventory": "",
    "last_rate_config": "",
    "last_rate_rule": "",
    "last_output_dir": "",
}


def default_settings_path() -> Path:
    """返回源码或EXE发布目录下的用户设置文件路径。"""
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return root / "config" / "user_settings.json"


def load_user_settings(path: str | Path | None = None) -> dict[str, str]:
    """读取最近使用配置；文件缺失或损坏时安全返回空配置。"""
    target = Path(path) if path is not None else default_settings_path()
    result = DEFAULT_SETTINGS.copy()
    if not target.is_file():
        return result
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return result
    if isinstance(payload, dict):
        for key in result:
            value = payload.get(key, "")
            result[key] = str(value) if value is not None else ""
    return result


def save_user_settings(
    settings: Mapping[str, object],
    path: str | Path | None = None,
) -> Path:
    """原子保存最近使用配置，不保存业务数据内容。"""
    target = Path(path) if path is not None else default_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = DEFAULT_SETTINGS.copy()
    payload.update(
        {
            key: str(settings.get(key, "") or "")
            for key in DEFAULT_SETTINGS
        }
    )
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def existing_initial_directory(
    configured_path: str | Path | None,
    fallback: str | Path | None = None,
) -> Path:
    """将最近文件路径还原为存在的父目录，不存在时降级到默认目录。"""
    if configured_path:
        candidate = Path(configured_path)
        directory = candidate if candidate.is_dir() else candidate.parent
        if directory.is_dir():
            return directory
    fallback_path = Path(fallback) if fallback is not None else Path.cwd()
    if fallback_path.is_file():
        fallback_path = fallback_path.parent
    return fallback_path if fallback_path.is_dir() else Path.cwd()
