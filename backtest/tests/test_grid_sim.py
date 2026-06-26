"""
网格成交仿真器不变量测试（不需要真实校准数据，验证逻辑正确性）。

注意：这些测试验证的是「模型自洽 + 行为方向正确」，不是「与 OKX 实际 PnL 吻合」——
后者需要 gridResult.csv 校准（本地暂无）。
"""
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BT_DIR = os.path.dirname(_TESTS_DIR)
if _BT_DIR not in sys.path:
    sys.path.insert(0, _BT_DIR)

from grid_sim import build_grid_levels, simulate_grid, apply_exit_rules


def bar(h, l, c, o=None):
    return {'high': h, 'low': l, 'close': c, 'open': o if o is not None else c}


BASE = dict(min_px=90.0, max_px=110.0, grid_num=10, run_type='2',
            sz=1000.0, lever=5.0, entry_px=100.0, tp_px=112.0, sl_px=88.0)


class TestLevels(unittest.TestCase):
    def test_geometric(self):
        L = build_grid_levels(90, 110, 10, '2')
        self.assertEqual(len(L), 11)
        self.assertAlmostEqual(L[0], 90)
        self.assertAlmostEqual(L[-1], 110)
        # 等比：相邻比值恒定
        r = L[1] / L[0]
        for i in range(1, len(L)):
            self.assertAlmostEqual(L[i] / L[i - 1], r, places=9)

    def test_arithmetic(self):
        L = build_grid_levels(90, 110, 10, '1')
        self.assertEqual(len(L), 11)
        for i in range(1, len(L)):
            self.assertAlmostEqual(L[i] - L[i - 1], 2.0, places=9)


class TestSimulateInvariants(unittest.TestCase):
    def test_self_consistency(self):
        bars = [bar(106, 99, 106), bar(106, 99, 100), bar(106, 99, 106), bar(106, 99, 100)]
        r = simulate_grid(BASE, bars, fee_rate=0.0)
        self.assertAlmostEqual(r['pnl'], r['realized'] + r['unrealized'] - r['fees'], places=6)
        self.assertAlmostEqual(r['pnl_ratio'], r['pnl'] / BASE['sz'], places=9)

    def test_oscillation_profits(self):
        # 在网格内反复上下穿越 → 已实现为正、有成交
        bars = [bar(106, 98, 106), bar(106, 98, 99),
                bar(106, 98, 106), bar(106, 98, 99),
                bar(106, 98, 106)]
        r = simulate_grid(BASE, bars, fee_rate=0.0)
        self.assertGreater(r['n_fills'], 0)
        self.assertGreater(r['realized'], 0.0)
        self.assertFalse(r['terminated'])

    def test_flat_no_fills(self):
        bars = [bar(100, 100, 100) for _ in range(5)]
        r = simulate_grid(BASE, bars, fee_rate=0.0)
        self.assertEqual(r['n_fills'], 0)
        self.assertAlmostEqual(r['realized'], 0.0, places=9)
        self.assertAlmostEqual(r['pnl'], 0.0, places=6)  # 初始库存成本=收盘=100 → 浮动0

    def test_crash_triggers_sl(self):
        bars = [bar(101, 85, 86)]  # low=85 < sl_px=88
        r = simulate_grid(BASE, bars, fee_rate=0.0)
        self.assertTrue(r['terminated'])
        self.assertEqual(r['exit_reason'], '止损终止')
        self.assertEqual(r['exit_px'], 88.0)
        self.assertLess(r['pnl'], 0.0)  # 初始多头库存按 88 平 → 亏

    def test_rally_triggers_tp(self):
        bars = [bar(113, 99, 113)]  # high=113 >= tp_px=112
        r = simulate_grid(BASE, bars, fee_rate=0.0)
        self.assertTrue(r['terminated'])
        self.assertEqual(r['exit_reason'], '止盈终止')
        self.assertEqual(r['exit_px'], 112.0)
        self.assertGreater(r['pnl'], 0.0)  # 初始多头库存按 112 平 → 赚

    def test_sl_priority_when_both_hit(self):
        # 同一 bar 同时触及 tp 和 sl，保守取 SL
        bars = [bar(113, 85, 100)]
        r = simulate_grid(BASE, bars, fee_rate=0.0)
        self.assertEqual(r['exit_reason'], '止损终止')

    def test_fees_reduce_pnl(self):
        bars = [bar(106, 98, 106), bar(106, 98, 99), bar(106, 98, 106)]
        r0 = simulate_grid(BASE, bars, fee_rate=0.0)
        rf = simulate_grid(BASE, bars, fee_rate=0.001)
        self.assertGreater(rf['fees'], 0.0)
        self.assertLess(rf['pnl'], r0['pnl'])


class TestExitRules(unittest.TestCase):
    CFG = dict(stop_profit=0.05, stop_loss=0.034, stop_risk_l1=0.00618, stop_risk_l2=0.01)

    def test_fixed_stop_loss(self):
        i, reason = apply_exit_rules([0.0, -0.01, -0.04], self.CFG)
        self.assertEqual(reason, '固定止损')
        self.assertEqual(i, 2)

    def test_fixed_stop_profit(self):
        i, reason = apply_exit_rules([0.0, 0.02, 0.06], self.CFG)
        self.assertEqual(reason, '固定止盈')

    def test_drawdown_l2(self):
        # pmax 到 0.025(>=0.02)，回落 >=0.01 → L2
        i, reason = apply_exit_rules([0.0, 0.025, 0.014], self.CFG)
        self.assertEqual(reason, '回撤止盈L2')

    def test_no_trigger(self):
        i, reason = apply_exit_rules([0.0, 0.005, -0.01, 0.008], self.CFG)
        self.assertIsNone(i)
        self.assertIsNone(reason)


if __name__ == '__main__':
    unittest.main(verbosity=2)
