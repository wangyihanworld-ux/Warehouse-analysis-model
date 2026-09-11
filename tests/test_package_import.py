from __future__ import annotations

import importlib
import unittest


class PackageImportTest(unittest.TestCase):
    def test_service_module_imports(self) -> None:
        module = importlib.import_module("warehouse_analysis.service")
        self.assertTrue(callable(module.run_analysis))

    def test_ui_module_imports(self) -> None:
        module = importlib.import_module("warehouse_analysis.ui.app")
        self.assertTrue(callable(module.main))

    def test_reporting_module_imports(self) -> None:
        module = importlib.import_module(
            "warehouse_analysis.reporting.excel_report"
        )
        self.assertTrue(callable(module.build_business_report))

    def test_analysis_modules_import(self) -> None:
        summary = importlib.import_module("warehouse_analysis.analysis.summary")
        sensitivity = importlib.import_module(
            "warehouse_analysis.analysis.sensitivity"
        )
        self.assertTrue(callable(summary.build_warehouse_summary))
        self.assertTrue(callable(sensitivity.build_fee_sensitivity))


if __name__ == "__main__":
    unittest.main()
