from __future__ import annotations

import json
import socket
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


_AUDIT_LOCK = threading.RLock()


def default_audit_database_path() -> Path:
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return root / "audit.db"


def initialize_audit_database(path: str | Path | None = None) -> Path:
    target = Path(path) if path is not None else default_audit_database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK, closing(sqlite3.connect(target)) as connection:
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    用户 TEXT NOT NULL,
                    操作类型 TEXT NOT NULL,
                    对象 TEXT,
                    修改前 TEXT,
                    修改后 TEXT,
                    时间 TEXT NOT NULL,
                    结果 TEXT NOT NULL,
                    IP地址 TEXT
                );
                CREATE TABLE IF NOT EXISTS rate_change_request (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    申请人 TEXT NOT NULL,
                    仓库名称 TEXT NOT NULL,
                    选择项 TEXT NOT NULL,
                    版本日期 TEXT NOT NULL,
                    修改前 TEXT NOT NULL,
                    修改后 TEXT NOT NULL,
                    行定位 TEXT,
                    状态 TEXT NOT NULL,
                    申请时间 TEXT NOT NULL,
                    审核人 TEXT,
                    审核时间 TEXT,
                    审核意见 TEXT
                );
                CREATE TABLE IF NOT EXISTS warehouse_name_mapping (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ERP仓库名称 TEXT NOT NULL,
                    费率仓库名称 TEXT NOT NULL,
                    确认时间 TEXT NOT NULL,
                    操作用户 TEXT NOT NULL
                );
                """
            )
    return target


def _ip_address() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return ""


def record_audit(
    *,
    user: str,
    action: str,
    target: str = "",
    before: object = "",
    after: object = "",
    result: str = "success",
    path: str | Path | None = None,
) -> int:
    database = initialize_audit_database(path)
    serialize = lambda value: (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    with _AUDIT_LOCK, closing(sqlite3.connect(database)) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_log
                (用户, 操作类型, 对象, 修改前, 修改后, 时间, 结果, IP地址)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user,
                    action,
                    target,
                    serialize(before),
                    serialize(after),
                    datetime.now().isoformat(timespec="seconds"),
                    result,
                    _ip_address(),
                ),
            )
            return int(cursor.lastrowid)


def query_audit(
    *,
    action: str | None = None,
    user: str | None = None,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    database = initialize_audit_database(path)
    clauses: list[str] = []
    params: list[str] = []
    if action:
        clauses.append("操作类型=?")
        params.append(action)
    if user:
        clauses.append("用户=?")
        params.append(user)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM audit_log"
                + where
                + " ORDER BY 时间 DESC, id DESC",
                params,
            )
        ]
    finally:
        connection.close()


def record_warehouse_mapping_confirmation(
    erp_warehouse: str,
    rate_warehouse: str,
    *,
    user: str,
    path: str | Path | None = None,
) -> int:
    """保存用户明确确认的仓库名称映射。"""
    database = initialize_audit_database(path)
    confirmed_at = datetime.now().isoformat(timespec="seconds")
    with _AUDIT_LOCK, closing(sqlite3.connect(database)) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO warehouse_name_mapping
                (ERP仓库名称, 费率仓库名称, 确认时间, 操作用户)
                VALUES (?, ?, ?, ?)
                """,
                (erp_warehouse, rate_warehouse, confirmed_at, user),
            )
    record_audit(
        user=user,
        action="用户确认仓库名称映射",
        target=erp_warehouse,
        before={"ERP仓库名称": erp_warehouse},
        after={"费率仓库名称": rate_warehouse, "确认时间": confirmed_at},
        result="成功",
        path=database,
    )
    return int(cursor.lastrowid)
