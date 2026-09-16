from __future__ import annotations

import re
import unittest

from warehouse_analysis.version import (
    PROJECT_NAME,
    RELEASE_DATE,
    VERSION,
    version_label,
)


class VersionTest(unittest.TestCase):
    def test_version_exists_and_has_semantic_format(self) -> None:
        self.assertEqual(VERSION, "1.5.6")
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_project_and_release_metadata_exist(self) -> None:
        self.assertEqual(PROJECT_NAME, "仓储分析模型工具")
        self.assertRegex(RELEASE_DATE, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(version_label(), "仓储分析模型工具 v1.5.6")


if __name__ == "__main__":
    unittest.main()
