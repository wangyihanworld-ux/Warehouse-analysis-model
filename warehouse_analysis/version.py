from __future__ import annotations

from datetime import date


PROJECT_NAME = "仓储分析模型工具"
VERSION = "1.5.6"
RELEASE_DATE = date.today().isoformat()


def version_label() -> str:
    """返回界面和报告统一使用的项目名称及版本。"""
    return f"{PROJECT_NAME} v{VERSION}"
