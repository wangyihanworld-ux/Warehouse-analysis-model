import unittest
import pandas as pd
from warehouse_analysis.core.calculation import run_engine
from warehouse_analysis.core.models import StatPeriod
from warehouse_analysis.analysis.summary import build_warehouse_summary

TX = ['仓库名称','物料名称','品类','方向','日期','数量','批次号']
OP = ['仓库名称','物料名称','品类','期初数量','期初批次入库基准日期','期初批次号']
RT = ['仓库名称','品类或物料','单价','计费方式说明','适用层级','生效日期']

class InventoryReconciliationTests(unittest.TestCase):
    def run_case(self, transactions, openings, start='2025-01-01', end='2025-03-31'):
        tx = pd.DataFrame(transactions, columns=TX)
        op = pd.DataFrame(openings, columns=OP)
        rt = pd.DataFrame([['W','全部',1,'cumulative','全部','2024-01-01']], columns=RT)
        periods = [StatPeriod('test', pd.Timestamp(start), pd.Timestamp(end))]
        result = run_engine(tx, op, rt, periods)
        summary = build_warehouse_summary(result, tx, op, rt, periods)
        return result, summary

    def test_sold_out_material_remains_in_earlier_snapshot(self):
        r, s = self.run_case([['W','A','C','出库','2025-02-10',100,'OUT']],
                            [['W','A','C',100,'2025-01-01','OPEN']])
        self.assertEqual(r.monthly_snapshot['月末库存量（吨）'].sum(), 100)
        self.assertAlmostEqual(s.iloc[0]['平均库存量（吨）'], 100/3)
        self.assertEqual(s.iloc[0]['库存勾稽差异（吨）'], 0)

    def test_rejected_request_is_not_an_executed_issue(self):
        r,s = self.run_case([['W','A','C','出库','2025-01-10',60,'BAD'],
                             ['W','A','C','出库','2025-01-11',20,'OK']],
                            [['W','A','C',50,'2025-01-01','OPEN']])
        row=s.iloc[0]
        self.assertEqual(row['期间请求出库量（吨）'],80)
        self.assertEqual(row['期间出库量（吨）'],20)
        self.assertEqual(row['期间拒绝出库量（吨）'],60)
        self.assertEqual(row['期末库存量（吨）'],30)
        self.assertEqual(row['库存勾稽差异（吨）'],0)

    def test_only_invalid_outbound_keeps_anomaly_and_zero_summary(self):
        r,s=self.run_case([['W','A','C','出库','2025-01-10',10,'BAD']],[])
        self.assertEqual(len(r.anomalies),1)
        self.assertTrue(r.batch_details.empty)
        self.assertEqual(s.iloc[0]['期间拒绝出库量（吨）'],10)
        self.assertEqual(s.iloc[0]['库存勾稽差异（吨）'],0)

    def test_period_flow_excludes_previous_outbound(self):
        _,s=self.run_case([['W','A','C','出库','2025-01-10',10,'OLD'],
                           ['W','A','C','出库','2025-02-10',20,'NEW']],
                          [['W','A','C',50,'2025-01-01','OPEN']],start='2025-02-01')
        self.assertEqual(s.iloc[0]['期初库存量（吨）'],40)
        self.assertEqual(s.iloc[0]['期间出库量（吨）'],20)
        self.assertEqual(s.iloc[0]['库存勾稽差异（吨）'],0)

    def test_cumulative_fee_is_not_period_accrual(self):
        r,_=self.run_case([], [['W','A','C',10,'2024-12-31','OPEN']],end='2025-01-10')
        self.assertEqual(r.warehouse_total.iloc[0]['系统计算仓储费（元）'],100)
