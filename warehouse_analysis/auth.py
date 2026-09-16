from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import sys
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROLE_ADMIN = "Admin"
ROLE_FINANCE = "Finance"
ROLE_VIEWER = "Viewer"
ROLES = {ROLE_ADMIN, ROLE_FINANCE, ROLE_VIEWER}

PERMISSIONS = {
    ROLE_ADMIN: {
        "manage_users",
        "manage_rates",
        "approve_rates",
        "run_analysis",
        "view_reports",
        "view_history",
        "view_logs",
        "view_dashboard",
    },
    ROLE_FINANCE: {
        "run_analysis",
        "view_reports",
        "view_history",
        "view_dashboard",
        "submit_rate_change",
    },
    ROLE_VIEWER: {
        "view_reports",
        "view_dashboard",
    },
}

_AUTH_LOCK = threading.RLock()
PBKDF2_ITERATIONS = 310_000


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    role: str
    status: str


def default_user_database_path() -> Path:
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return root / "warehouse_users.db"


def _hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < 8:
        raise ValueError("密码长度不能少于8位")
    actual_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        actual_salt,
        PBKDF2_ITERATIONS,
    )
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
        f"{actual_salt.hex()}${digest.hex()}"
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), expected_hex)
    except (ValueError, TypeError):
        return False


def initialize_user_database(path: str | Path | None = None) -> Path:
    target = Path(path) if path is not None else default_user_database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _AUTH_LOCK, closing(sqlite3.connect(target)) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    用户名 TEXT NOT NULL UNIQUE,
                    密码哈希 TEXT NOT NULL,
                    角色 TEXT NOT NULL,
                    状态 TEXT NOT NULL DEFAULT '启用',
                    创建时间 TEXT NOT NULL,
                    最后登录时间 TEXT
                )
                """
            )
    return target


def has_permission(user: AuthUser, permission: str) -> bool:
    return (
        user.status == "启用"
        and permission in PERMISSIONS.get(user.role, set())
    )


def require_permission(user: AuthUser, permission: str) -> None:
    if not has_permission(user, permission):
        raise PermissionError(
            f"用户{user.username}（{user.role}）没有权限：{permission}"
        )


def create_user(
    username: str,
    password: str,
    role: str,
    *,
    actor: AuthUser | None = None,
    path: str | Path | None = None,
) -> AuthUser:
    if actor is not None:
        require_permission(actor, "manage_users")
    normalized = username.strip()
    if not normalized:
        raise ValueError("用户名不能为空")
    if role not in ROLES:
        raise ValueError(f"不支持的角色：{role}")
    target = initialize_user_database(path)
    password_hash = _hash_password(password)
    with _AUTH_LOCK, closing(sqlite3.connect(target)) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO users
                (用户名, 密码哈希, 角色, 状态, 创建时间)
                VALUES (?, ?, ?, '启用', ?)
                """,
                (
                    normalized,
                    password_hash,
                    role,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            user_id = int(cursor.lastrowid)
    return AuthUser(user_id, normalized, role, "启用")


def authenticate(
    username: str,
    password: str,
    *,
    path: str | Path | None = None,
) -> AuthUser | None:
    target = initialize_user_database(path)
    with _AUTH_LOCK, closing(sqlite3.connect(target)) as connection:
        row = connection.execute(
            "SELECT id, 用户名, 密码哈希, 角色, 状态 FROM users WHERE 用户名=?",
            (username.strip(),),
        ).fetchone()
        if (
            row is None
            or row[4] != "启用"
            or not _verify_password(password, row[2])
        ):
            return None
        with connection:
            connection.execute(
                "UPDATE users SET 最后登录时间=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), row[0]),
            )
        return AuthUser(int(row[0]), str(row[1]), str(row[3]), str(row[4]))


def list_users(
    actor: AuthUser,
    *,
    path: str | Path | None = None,
) -> list[dict[str, object]]:
    require_permission(actor, "manage_users")
    target = initialize_user_database(path)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, 用户名, 角色, 状态, 创建时间, 最后登录时间
                FROM users ORDER BY 用户名
                """
            )
        ]
    finally:
        connection.close()


def set_user_status(
    user_id: int,
    status: str,
    *,
    actor: AuthUser,
    path: str | Path | None = None,
) -> None:
    require_permission(actor, "manage_users")
    if status not in {"启用", "停用"}:
        raise ValueError("状态只能为启用或停用")
    target = initialize_user_database(path)
    with closing(sqlite3.connect(target)) as connection:
        with connection:
            connection.execute(
                "UPDATE users SET 状态=? WHERE id=?", (status, user_id)
            )


def ensure_default_admin(path: str | Path | None = None) -> AuthUser:
    """首次启动创建管理员；部署后应立即修改默认密码。"""
    target = initialize_user_database(path)
    with closing(sqlite3.connect(target)) as connection:
        row = connection.execute(
            "SELECT id, 用户名, 角色, 状态 FROM users WHERE 用户名='admin'"
        ).fetchone()
    if row:
        return AuthUser(int(row[0]), str(row[1]), str(row[2]), str(row[3]))
    password = os.environ.get("WAREHOUSE_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError(
            "公开版首次启动前必须设置环境变量 WAREHOUSE_ADMIN_PASSWORD"
        )
    return create_user("admin", password, ROLE_ADMIN, path=target)
