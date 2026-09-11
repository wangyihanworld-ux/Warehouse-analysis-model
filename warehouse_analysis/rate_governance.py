from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from warehouse_analysis.audit import (
    default_audit_database_path,
    initialize_audit_database,
    record_audit,
)
from warehouse_analysis.auth import AuthUser, has_permission, require_permission
from warehouse_analysis.rate_manager import (
    DEFAULT_RATE_DATABASE,
    load_rate_database,
    update_rate_rule,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _locate_rate(
    selection: str,
    effective_date: object,
    database_path: str | Path,
    row_match: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    data = load_rate_database(database_path)
    version = pd.Timestamp(effective_date).normalize()
    mask = (data["选择项"] == selection) & (data["生效日期"] == version)
    for field, value in (row_match or {}).items():
        mask &= data[field].isna() if pd.isna(value) else data[field].astype(str).eq(str(value))
    rows = data.loc[mask]
    if rows.empty:
        raise ValueError(f"未找到规则版本：{selection} / {version.date()}")
    return rows.iloc[0].to_dict()


def submit_rate_change(
    user: AuthUser,
    *,
    selection: str,
    effective_date: object,
    updates: Mapping[str, Any],
    database_path: str | Path = DEFAULT_RATE_DATABASE,
    audit_path: str | Path | None = None,
    row_match: Mapping[str, Any] | None = None,
) -> int:
    """提交费率变更申请；Finance进入待审核，Admin可随后审核执行。"""
    if not (
        has_permission(user, "submit_rate_change")
        or has_permission(user, "manage_rates")
    ):
        raise PermissionError(f"用户{user.username}没有提交费率变更权限")
    before = _locate_rate(selection, effective_date, database_path, row_match)
    warehouse = str(before["仓库名称"])
    requested = dict(updates)
    database = initialize_audit_database(audit_path)
    now = datetime.now().isoformat(timespec="seconds")
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO rate_change_request
                (申请人, 仓库名称, 选择项, 版本日期, 修改前, 修改后,
                 行定位, 状态, 申请时间)
                VALUES (?, ?, ?, ?, ?, ?, ?, '待审核', ?)
                """,
                (
                    user.username,
                    warehouse,
                    selection,
                    str(pd.Timestamp(effective_date).date()),
                    _json(before),
                    _json(requested),
                    _json(dict(row_match or {})),
                    now,
                ),
            )
            request_id = int(cursor.lastrowid)
    record_audit(
        user=user.username,
        action="费率变更申请",
        target=selection,
        before=before,
        after=requested,
        result="待审核",
        path=database,
    )
    return request_id


def approve_rate_change(
    admin: AuthUser,
    request_id: int,
    *,
    database_path: str | Path = DEFAULT_RATE_DATABASE,
    audit_path: str | Path | None = None,
    comment: str = "",
) -> int:
    """管理员批准待审核申请，并通过既有费率管理接口实施修改。"""
    require_permission(admin, "approve_rates")
    database = initialize_audit_database(audit_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM rate_change_request WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"费率变更申请不存在：{request_id}")
        if row["状态"] != "待审核":
            raise ValueError(f"费率变更申请状态不是待审核：{row['状态']}")
        updates = json.loads(row["修改后"])
        row_match = json.loads(row["行定位"] or "{}")
        count = update_rate_rule(
            row["选择项"],
            row["版本日期"],
            updates,
            database_path,
            row_match=row_match,
        )
        with connection:
            connection.execute(
                """
                UPDATE rate_change_request
                SET 状态='已通过', 审核人=?, 审核时间=?, 审核意见=?
                WHERE id=?
                """,
                (
                    admin.username,
                    datetime.now().isoformat(timespec="seconds"),
                    comment,
                    request_id,
                ),
            )
    after = _locate_rate(row["选择项"], row["版本日期"], database_path, row_match)
    record_audit(
        user=admin.username,
        action="费率修改",
        target=row["选择项"],
        before=json.loads(row["修改前"]),
        after=after,
        result="已通过",
        path=database,
    )
    return count


def reject_rate_change(
    admin: AuthUser,
    request_id: int,
    *,
    audit_path: str | Path | None = None,
    comment: str = "",
) -> None:
    """管理员拒绝待审核申请，不触碰费率数据库。"""
    require_permission(admin, "approve_rates")
    database = initialize_audit_database(audit_path)
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT 选择项, 状态 FROM rate_change_request WHERE id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"费率变更申请不存在：{request_id}")
        if row[1] != "待审核":
            raise ValueError(f"费率变更申请状态不是待审核：{row[1]}")
        with connection:
            connection.execute(
                """
                UPDATE rate_change_request
                SET 状态='已拒绝', 审核人=?, 审核时间=?, 审核意见=?
                WHERE id=?
                """,
                (
                    admin.username,
                    datetime.now().isoformat(timespec="seconds"),
                    comment,
                    request_id,
                ),
            )
    record_audit(
        user=admin.username,
        action="费率变更审核",
        target=row[0],
        result="已拒绝",
        after={"审核意见": comment},
        path=database,
    )


def query_rate_requests(
    *,
    status: str | None = None,
    audit_path: str | Path | None = None,
) -> list[dict[str, object]]:
    database = initialize_audit_database(audit_path)
    sql = "SELECT * FROM rate_change_request"
    params: tuple[object, ...] = ()
    if status:
        sql += " WHERE 状态=?"
        params = (status,)
    sql += " ORDER BY 申请时间 DESC, id DESC"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def query_rate_version_changes(
    audit_path: str | Path | None = None,
) -> list[dict[str, object]]:
    database = initialize_audit_database(
        audit_path or default_audit_database_path()
    )
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT 时间 AS 日期, 对象 AS 仓库规则,
                       修改前 AS 旧值, 修改后 AS 新值, 用户 AS 修改人, 结果
                FROM audit_log
                WHERE 操作类型='费率修改'
                ORDER BY 时间 DESC, id DESC
                """
            )
        ]
    finally:
        connection.close()
