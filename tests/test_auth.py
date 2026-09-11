import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from warehouse_analysis.auth import (
    AuthUser,
    ROLE_ADMIN,
    ROLE_FINANCE,
    authenticate,
    create_user,
    set_user_status,
)


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "users.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_user_create_and_password_is_hashed(self):
        user = create_user("finance01", "Secure@123", ROLE_FINANCE, path=self.database)
        self.assertEqual(user.username, "finance01")
        with closing(sqlite3.connect(self.database)) as connection:
            password_hash = connection.execute(
                "SELECT 密码哈希 FROM users WHERE 用户名='finance01'"
            ).fetchone()[0]
        self.assertNotEqual(password_hash, "Secure@123")
        self.assertTrue(password_hash.startswith("pbkdf2_sha256$"))

    def test_login_success(self):
        create_user("admin01", "Secure@123", ROLE_ADMIN, path=self.database)
        self.assertEqual(
            authenticate("admin01", "Secure@123", path=self.database).role,
            ROLE_ADMIN,
        )

    def test_wrong_password_is_rejected(self):
        create_user("admin01", "Secure@123", ROLE_ADMIN, path=self.database)
        self.assertIsNone(
            authenticate("admin01", "wrong-password", path=self.database)
        )

    def test_invalid_short_password(self):
        with self.assertRaisesRegex(ValueError, "8"):
            create_user("user", "short", ROLE_FINANCE, path=self.database)

    def test_duplicate_username_is_rejected(self):
        create_user("same", "Secure@123", ROLE_FINANCE, path=self.database)
        with self.assertRaises(sqlite3.IntegrityError):
            create_user("same", "Secure@456", ROLE_FINANCE, path=self.database)

    def test_disabled_user_cannot_login(self):
        created = create_user("finance02", "Secure@123", ROLE_FINANCE, path=self.database)
        admin = AuthUser(99, "admin", ROLE_ADMIN, "启用")
        set_user_status(created.id, "停用", actor=admin, path=self.database)
        self.assertIsNone(authenticate("finance02", "Secure@123", path=self.database))


if __name__ == "__main__":
    unittest.main()
