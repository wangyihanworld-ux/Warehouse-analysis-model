from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


_DB_LOCK = threading.RLock()


def default_database_path() -> Path:
    """返回源码或EXE发布目录中的SQLite数据库路径。"""
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return root / "warehouse_analysis.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS run_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    运行时间 TEXT NOT NULL,
    版本 TEXT NOT NULL,
    运行类型 TEXT NOT NULL,
    输入文件 TEXT NOT NULL,
    期初文件 TEXT,
    开始日期 TEXT NOT NULL,
    结束日期 TEXT NOT NULL,
    状态 TEXT NOT NULL,
    耗时 REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS warehouse_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    仓库名称 TEXT NOT NULL,
    统计期间 TEXT NOT NULL,
    期末库存 REAL NOT NULL DEFAULT 0,
    平均库存 REAL NOT NULL DEFAULT 0,
    平均存放天数 REAL NOT NULL DEFAULT 0,
    仓储费 REAL NOT NULL DEFAULT 0,
    report_filename TEXT NOT NULL DEFAULT '',
    报告路径 TEXT NOT NULL,
    状态 TEXT NOT NULL DEFAULT 'success',
    FOREIGN KEY(run_id) REFERENCES run_history(id)
);
CREATE TABLE IF NOT EXISTS rate_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    仓库名称 TEXT NOT NULL,
    计费规则 TEXT NOT NULL,
    版本日期 TEXT,
    单价 REAL,
    FOREIGN KEY(run_id) REFERENCES run_history(id)
);
CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    时间 TEXT NOT NULL,
    仓库 TEXT,
    错误模块 TEXT NOT NULL,
    错误类型 TEXT NOT NULL,
    错误描述 TEXT NOT NULL,
    解决建议 TEXT,
    FOREIGN KEY(run_id) REFERENCES run_history(id)
);
CREATE TABLE IF NOT EXISTS dashboard_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    时间 TEXT NOT NULL,
    期间 TEXT NOT NULL,
    仓库 TEXT NOT NULL,
    库存 REAL NOT NULL DEFAULT 0,
    平均库存 REAL NOT NULL DEFAULT 0,
    仓储费 REAL NOT NULL DEFAULT 0,
    平均天数 REAL NOT NULL DEFAULT 0,
    FOREIGN KEY(run_id) REFERENCES run_history(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_snapshot_result
ON dashboard_snapshot(run_id, 仓库, 期间);
CREATE INDEX IF NOT EXISTS idx_result_warehouse_period
ON warehouse_result(仓库名称, 统计期间);
CREATE INDEX IF NOT EXISTS idx_run_dates
ON run_history(开始日期, 结束日期);
CREATE TRIGGER IF NOT EXISTS trg_warehouse_result_dashboard
AFTER INSERT ON warehouse_result
WHEN NEW.状态 = 'success'
BEGIN
    INSERT OR REPLACE INTO dashboard_snapshot
    (run_id, 时间, 期间, 仓库, 库存, 平均库存, 仓储费, 平均天数)
    SELECT NEW.run_id, 运行时间, NEW.统计期间, NEW.仓库名称,
           NEW.期末库存, NEW.平均库存, NEW.仓储费, NEW.平均存放天数
    FROM run_history WHERE id = NEW.run_id;
END;
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path, timeout=30)
        connection.execute("PRAGMA schema_version").fetchone()
        return connection
    except sqlite3.DatabaseError:
        try:
            connection.close()
        except UnboundLocalError:
            pass
        if path.exists():
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.move(path, path.with_suffix(f".corrupt-{stamp}.db"))
        return sqlite3.connect(path, timeout=30)


def initialize_database(path: str | Path | None = None) -> Path:
    """创建数据库及四张历史表；损坏文件会保留备份后重建。"""
    target = Path(path) if path is not None else default_database_path()
    with _DB_LOCK:
        connection = _connect(target)
        try:
            connection.executescript(SCHEMA)
            result_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(warehouse_result)"
                ).fetchall()
            }
            if "report_filename" not in result_columns:
                connection.execute(
                    "ALTER TABLE warehouse_result "
                    "ADD COLUMN report_filename TEXT NOT NULL DEFAULT ''"
                )
            connection.commit()
        finally:
            connection.close()
    return target


def create_run(
    *,
    version: str,
    run_type: str,
    input_file: str | Path,
    opening_file: str | Path | None,
    start_date: object,
    end_date: object,
    status: str = "running",
    path: str | Path | None = None,
) -> int:
    target = initialize_database(path)
    with _DB_LOCK, closing(sqlite3.connect(target, timeout=30)) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO run_history
                (运行时间, 版本, 运行类型, 输入文件, 期初文件,
                 开始日期, 结束日期, 状态, 耗时)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    version,
                    run_type,
                    str(input_file),
                    str(opening_file or ""),
                    str(start_date)[:10],
                    str(end_date)[:10],
                    status,
                ),
            )
            return int(cursor.lastrowid)


def finish_run(
    run_id: int,
    status: str,
    elapsed_seconds: float,
    path: str | Path | None = None,
) -> None:
    target = initialize_database(path)
    with _DB_LOCK, closing(sqlite3.connect(target, timeout=30)) as connection:
        with connection:
            connection.execute(
                "UPDATE run_history SET 状态=?, 耗时=? WHERE id=?",
                (status, float(elapsed_seconds), run_id),
            )


def save_warehouse_result(
    run_id: int,
    *,
    warehouse: str,
    period: str,
    ending_inventory: float,
    average_inventory: float,
    average_days: float,
    fee: float,
    report_path: str | Path,
    status: str = "success",
    path: str | Path | None = None,
) -> None:
    target = initialize_database(path)
    with _DB_LOCK, closing(sqlite3.connect(target, timeout=30)) as connection:
        with connection:
            connection.execute(
            """
            INSERT INTO warehouse_result
            (run_id, 仓库名称, 统计期间, 期末库存, 平均库存,
             平均存放天数, 仓储费, report_filename, 报告路径, 状态)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                run_id,
                warehouse,
                period,
                float(ending_inventory),
                float(average_inventory),
                float(average_days),
                float(fee),
                Path(report_path).name,
                str(report_path),
                status,
                ),
            )


def save_rate_snapshot(
    run_id: int,
    *,
    warehouse: str,
    rule: str,
    version_date: object,
    price: float,
    path: str | Path | None = None,
) -> None:
    target = initialize_database(path)
    with _DB_LOCK, closing(sqlite3.connect(target, timeout=30)) as connection:
        with connection:
            connection.execute(
            """
            INSERT INTO rate_snapshot
            (run_id, 仓库名称, 计费规则, 版本日期, 单价)
            VALUES (?, ?, ?, ?, ?)
            """,
                (
                run_id,
                warehouse,
                rule,
                str(version_date)[:10],
                float(price),
                ),
            )


def save_error(
    *,
    warehouse: str,
    module: str,
    error_type: str,
    description: str,
    suggestion: str,
    run_id: int | None = None,
    path: str | Path | None = None,
) -> None:
    target = initialize_database(path)
    with _DB_LOCK, closing(sqlite3.connect(target, timeout=30)) as connection:
        with connection:
            connection.execute(
            """
            INSERT INTO error_log
            (run_id, 时间, 仓库, 错误模块, 错误类型, 错误描述, 解决建议)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                run_id,
                datetime.now().isoformat(timespec="seconds"),
                warehouse,
                module,
                error_type,
                description,
                suggestion,
                ),
            )


def query_history(
    *,
    date: str | None = None,
    warehouse: str | None = None,
    month: str | None = None,
    status: str | None = None,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """按日期、仓库、月份和状态查询企业运行历史。"""
    target = initialize_database(path)
    clauses: list[str] = []
    parameters: list[object] = []
    if date:
        clauses.append("rh.运行时间 LIKE ?")
        parameters.append(f"{date}%")
    if warehouse:
        clauses.append("wr.仓库名称 LIKE ?")
        parameters.append(f"%{warehouse}%")
    if month:
        clauses.append("wr.统计期间 LIKE ?")
        parameters.append(f"{month}%")
    if status:
        clauses.append("COALESCE(wr.状态, rh.状态) = ?")
        parameters.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = (
        """
        SELECT rh.id, rh.运行时间, rh.版本, rh.运行类型,
               rh.开始日期, rh.结束日期, rh.状态 AS 运行状态,
               wr.仓库名称, wr.统计期间, wr.仓储费,
               wr.report_filename, wr.报告路径, wr.状态 AS 仓库状态
        FROM run_history rh
        LEFT JOIN warehouse_result wr ON wr.run_id = rh.id
        """
        + where
        + " ORDER BY rh.运行时间 DESC, wr.仓库名称"
    )
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(sql, parameters).fetchall()
        ]
    finally:
        connection.close()
