from __future__ import annotations

import logging
from pathlib import Path

from warehouse_analysis.version import PROJECT_NAME, VERSION


LOGGER_NAME = "warehouse_analysis"
LOG_FILE_NAME = "warehouse_analysis.log"


def configure_logging(log_dir: str | Path | None = None) -> logging.Logger:
    """初始化项目统一日志并返回命名logger。

    默认写入当前工作目录下的logs/warehouse_analysis.log。重复调用不会
    为同一文件增加重复Handler。
    """
    directory = Path(log_dir) if log_dir is not None else Path.cwd() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    log_path = (directory / LOG_FILE_NAME).resolve()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in logger.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename).resolve() == log_path
        ):
            return logger

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.info("日志初始化完成 | 项目=%s | 版本=%s", PROJECT_NAME, VERSION)
    return logger
