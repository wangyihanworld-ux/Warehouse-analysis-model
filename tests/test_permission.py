import unittest

from warehouse_analysis.auth import (
    AuthUser,
    ROLE_ADMIN,
    ROLE_FINANCE,
    ROLE_VIEWER,
    has_permission,
)


def user(role):
    return AuthUser(1, role.lower(), role, "启用")


class PermissionTest(unittest.TestCase):
    def test_admin_permissions(self):
        self.assertTrue(has_permission(user(ROLE_ADMIN), "manage_users"))
        self.assertTrue(has_permission(user(ROLE_ADMIN), "manage_rates"))
        self.assertTrue(has_permission(user(ROLE_ADMIN), "view_logs"))

    def test_finance_permissions(self):
        self.assertTrue(has_permission(user(ROLE_FINANCE), "run_analysis"))
        self.assertTrue(has_permission(user(ROLE_FINANCE), "submit_rate_change"))
        self.assertFalse(has_permission(user(ROLE_FINANCE), "manage_rates"))

    def test_viewer_permissions(self):
        self.assertTrue(has_permission(user(ROLE_VIEWER), "view_reports"))
        self.assertTrue(has_permission(user(ROLE_VIEWER), "view_dashboard"))
        self.assertFalse(has_permission(user(ROLE_VIEWER), "run_analysis"))


if __name__ == "__main__":
    unittest.main()
