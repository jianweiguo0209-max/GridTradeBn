"""grid_engine 退出逻辑单测：复刻实盘 calc_loss_or_profit 优先级。"""
import os
import sys
import unittest

import pandas as pd

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BT_DIR = os.path.dirname(_TESTS_DIR)
if _BT_DIR not in sys.path:
    sys.path.insert(0, _BT_DIR)

from grid_engine import _apply_exit  # noqa: E402

CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
       'fundingRate_stop_loss': 0.0015}


def _df(nvs, funding=None):
    n = len(nvs)
    return pd.DataFrame({
        'candle_begin_time': pd.date_range('2026-01-01', periods=n, freq='1min'),
        'net_value': nvs, 'hold_num': [0.0] * n, 'close': [1.0] * n,
        'fundingRate': funding if funding is not None else [0.0] * n,
    })


class TestApplyExit(unittest.TestCase):
    def test_fixed_stop_loss(self):
        df, reason, blown = _apply_exit(_df([1.0, 0.98, 0.95]), 1000, 0.0005, CFG)
        self.assertEqual(reason, '固定止损')
        self.assertEqual(len(df), 3)  # 截断到首次触发(idx2)含该行

    def test_chandelier_trailing(self):
        df, reason, blown = _apply_exit(_df([1.0, 1.05, 1.02]), 1000, 0.0005, CFG)
        self.assertEqual(reason, '连续回撤止盈')

    def test_priority_fixed_over_chandelier(self):
        # 同 bar 两者都满足 → 固定止损优先
        df, reason, blown = _apply_exit(_df([1.0, 1.05, 0.96]), 1000, 0.0005, CFG)
        self.assertEqual(reason, '固定止损')

    def test_funding_stop(self):
        df, reason, blown = _apply_exit(_df([1.0, 1.0, 1.0], funding=[0.0, 0.002, 0.0]), 1000, 0.0005, CFG)
        self.assertEqual(reason, '资金费率止损')
        self.assertEqual(len(df), 2)

    def test_pv_stop(self):
        # pv 量能爆增信号 + pnlRatio<-0.015 → pv主动止损
        df = _df([1.0, 0.99, 0.98])  # idx2 pr=-0.02 < -0.015
        pv = pd.DataFrame({'candle_begin_time': df['candle_begin_time'], 'pv_spike': [0, 0, 1]})
        out, reason, blown = _apply_exit(df, 1000, 0.0005, CFG, pv_spike_df=pv)
        self.assertEqual(reason, 'pv主动止损')

    def test_pv_not_triggered_when_profitable(self):
        # pv 量能爆增但未亏损(pnlRatio≥-0.015) → 不触发 pv
        df = _df([1.0, 1.0, 1.0])
        pv = pd.DataFrame({'candle_begin_time': df['candle_begin_time'], 'pv_spike': [0, 1, 1]})
        out, reason, blown = _apply_exit(df, 1000, 0.0005, CFG, pv_spike_df=pv)
        self.assertIsNone(reason)

    def test_no_trigger(self):
        # 峰值 0.005 < floor 0.00618 → chandelier 不触发；无固定止损/资金费
        df, reason, blown = _apply_exit(_df([1.0, 1.005, 1.004]), 1000, 0.0005, CFG)
        self.assertIsNone(reason)
        self.assertFalse(blown)
        self.assertEqual(len(df), 3)

    def test_stop_cfg_none_runs_to_end(self):
        # 无 stop_cfg：不套退出（只查爆仓），跑到末尾
        df, reason, blown = _apply_exit(_df([1.0, 0.9, 0.95]), 1000, 0.0005, None)
        self.assertIsNone(reason)
        self.assertEqual(len(df), 3)

    def test_liquidation(self):
        df, reason, blown = _apply_exit(_df([1.0, 0.5, 0.02]), 1000, 0.0005, None)
        self.assertEqual(reason, '爆仓')
        self.assertTrue(blown)
        self.assertEqual(df['net_value'].iloc[-1], 0.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
