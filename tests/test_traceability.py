import unittest
import tempfile
from pathlib import Path
from warehouse_analysis.demo import build_synthetic_inputs, DEMO_PERIOD
from warehouse_analysis.core.calculation import run_engine
from warehouse_analysis.reproducibility import write_manifest


class TraceabilityTests(unittest.TestCase):
    def test_allocation_has_input_and_output_rows(self):
        tx, op, rates = build_synthetic_inputs()
        result = run_engine(tx, op, rates, [DEMO_PERIOD])
        issued = result.batch_details.query("状态 == '已出库'")
        self.assertFalse(issued['入库源数据行号'].isna().any())
        self.assertFalse(issued['出库源数据行号'].isna().any())
        self.assertEqual(result.conservation['数量差异（吨）'].sum(), 0)

    def test_manifest_changes_with_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'input.csv'; path.write_text('quantity\n10', encoding='utf-8')
            first = write_manifest(Path(folder)/'manifest.json', [path], {'rate_mode':'endpoint'})
            path.write_text('quantity\n20', encoding='utf-8')
            second = write_manifest(Path(folder)/'manifest.json', [path], {'rate_mode':'endpoint'})
            self.assertNotEqual(first['inputs'], second['inputs'])
            self.assertTrue(first['source_sha256'])
