"""backtest_run 的纯逻辑单测（持仓 bar 切片 + 聚合）。整链路由真实跑验证。"""
import os
import shutil
import sys
import tempfile
import unittest

import pandas as pd

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BT_DIR = os.path.dirname(_TESTS_DIR)
if _BT_DIR not in sys.path:
    sys.path.insert(0, _BT_DIR)

import backtest_run as BT
import prewarm
from cache import ParquetCache


class TestHoldingBars(unittest.TestCase):
    def _series(self):
        # UTC 时间序列，每小时一根
        t = pd.date_range('2024-01-01 00:00:00', periods=48, freq='1H')
        return pd.DataFrame({'candle_begin_time': t, 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0})

    def test_holding_window_utc_offset(self):
        s = self._series()
        # run_time 是 UTC+8 墙钟；utc_offset=8 → 持仓 [run_time, run_time+12H)
        rt = pd.Timestamp('2024-01-01 20:00:00')  # = UTC 12:00
        sub = BT.holding_bars(s, rt, '12H', utc_offset=8)
        # 期望 UTC 12:00 .. 23:00 共 12 根（local 20:00..31:00）
        self.assertEqual(len(sub), 12)
        self.assertEqual(sub.iloc[0]['candle_begin_time'], pd.Timestamp('2024-01-01 12:00:00'))
        self.assertEqual(sub.iloc[-1]['candle_begin_time'], pd.Timestamp('2024-01-01 23:00:00'))

    def test_empty_when_no_bars(self):
        s = self._series()
        sub = BT.holding_bars(s, pd.Timestamp('2030-01-01 00:00:00'), '12H', utc_offset=8)
        self.assertEqual(len(sub), 0)


class TestLoad1HHolding(unittest.TestCase):
    """1H 动态缓存：缓存 miss 时按需补取，只读窗口涉及的天，再切持仓段。"""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = ParquetCache(self.tmp)
        self._orig = prewarm._fetch_symbol_candles

    def tearDown(self):
        prewarm._fetch_symbol_candles = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_1H(self, sym, start, periods):
        t = pd.date_range(start, periods=periods, freq='1H')
        df = pd.DataFrame({'symbol': sym, 'candle_begin_time': t, 'open': 1.0, 'high': 1.0,
                           'low': 1.0, 'close': 1.0, 'vol': 0.0, 'volCcy': 0.0, 'quote_volume': 0.0})
        for d, g in df.groupby(df['candle_begin_time'].dt.strftime('%Y-%m-%d')):
            self.cache.write('1H', sym, d, g.reset_index(drop=True))

    def test_fetches_1H_on_miss_then_slices(self):
        sym = 'AAA-USDT-SWAP'
        calls = {'n': 0}

        def fake_fetch(cache, symbol, lo, hi, bar, proxies):
            calls['n'] += 1
            self.assertEqual(bar, '1H')        # 必须按 1H 取数
            self._write_1H(symbol, '2024-01-01 00:00:00', 72)  # 模拟取数：写 3 天 1H
            return symbol, 72, 'fetched'
        prewarm._fetch_symbol_candles = fake_fetch

        rt = pd.Timestamp('2024-01-01 20:00:00')  # UTC+8 墙钟 → 持仓 UTC 12:00..23:00
        sub = BT.load_1H_holding(self.cache, sym, rt, '12H', utc_offset=8, proxies=None)
        self.assertEqual(calls['n'], 1)        # 缓存空 → 触发了补取
        self.assertEqual(len(sub), 12)         # 持仓窗口 12 根
        self.assertEqual(sub.iloc[0]['candle_begin_time'], pd.Timestamp('2024-01-01 12:00:00'))
        self.assertEqual(sub.iloc[-1]['candle_begin_time'], pd.Timestamp('2024-01-01 23:00:00'))

    def test_empty_when_fetch_yields_no_data(self):
        def fake_fetch(cache, symbol, lo, hi, bar, proxies):
            return symbol, 0, 'empty'          # 补取后仍无数据
        prewarm._fetch_symbol_candles = fake_fetch
        sub = BT.load_1H_holding(self.cache, 'DEAD-USDT-SWAP',
                                 pd.Timestamp('2024-01-01 20:00:00'), '12H', 8, None)
        self.assertEqual(len(sub), 0)


class TestSummarize(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(BT.summarize(pd.DataFrame()), {'n_grids': 0})

    def test_aggregate(self):
        df = pd.DataFrame([
            {'run_time': pd.Timestamp('2024-01-01 00:00:00'), 'offset': 0, 'pnl_ratio': 0.10, 'exit_reason': 'A'},
            {'run_time': pd.Timestamp('2024-01-01 12:00:00'), 'offset': 0, 'pnl_ratio': -0.05, 'exit_reason': 'B'},
            {'run_time': pd.Timestamp('2024-01-01 01:00:00'), 'offset': 1, 'pnl_ratio': 0.20, 'exit_reason': 'A'},
        ])
        s = BT.summarize(df)
        self.assertEqual(s['n_grids'], 3)
        self.assertAlmostEqual(s['win_rate'], 2 / 3)
        # offset0 复利: 1.10*0.95=1.045; offset1: 1.20 → 组合等权 (1.045+1.20)/2-1
        self.assertAlmostEqual(s['offset_equity'][0], 1.10 * 0.95, places=9)
        self.assertAlmostEqual(s['offset_equity'][1], 1.20, places=9)
        self.assertAlmostEqual(s['portfolio_return'], (1.045 + 1.20) / 2 - 1, places=9)
        self.assertEqual(s['exit_reasons']['A'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
