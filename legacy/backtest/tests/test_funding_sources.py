"""
funding 多源(REST + swaprate CSV)兼容逻辑单测。

覆盖：
- okx_history._parse_swaprate_zip : OKX 下载中心 swaprate zip(GBK CSV)解析 + real_funding_rate 映射
- prewarm._merge_funding          : swaprate(早期) + REST(近期) 合并去重，重叠处 REST 优先
- backtest_run._funding_missing   : 持仓窗口内无 funding 观测 → 标记 True（防偏乐观结果被当真值）

运行：  TZ=Asia/Shanghai ../.venv/bin/python -m unittest discover -s tests -v
"""
import io
import os
import sys
import unittest
import zipfile

import pandas as pd

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BT_DIR = os.path.dirname(_TESTS_DIR)
if _BT_DIR not in sys.path:
    sys.path.insert(0, _BT_DIR)

import okx_history as H  # noqa: E402
import prewarm as P  # noqa: E402
import backtest_run as BT  # noqa: E402


def _make_swaprate_zip(rows, inst='BTC-USDT-SWAP', day='2023-01-15'):
    """构造与 OKX 下载中心同构的 swaprate zip：GBK 编码、表头中英混排、列序
    [contract_type, funding_rate(预测), real_funding_rate(实际), funding_time(ms)]。
    rows: list of (funding_rate, real_funding_rate, funding_time_ms)。"""
    header = 'contract_type/合约类型,funding_rate/预测下一周期费率,real_funding_rate/本周期真实费率,funding_time/下一周期时间戳'
    lines = [header] + ['SWAP,%s,%s,%d' % (fr, rfr, ts) for (fr, rfr, ts) in rows]
    csv_bytes = ('\n'.join(lines)).encode('gbk')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('%s-swaprate-%s.csv' % (inst, day), csv_bytes)
    return buf.getvalue()


class TestParseSwaprateZip(unittest.TestCase):
    def test_maps_real_funding_rate_and_decodes_gbk(self):
        zb = _make_swaprate_zip([
            (0.0002076, 0.0004659, 1673798400000),
            (0.0010839, -0.0008055, 1673769600000),
        ])
        df = H._parse_swaprate_zip(zb, 'BTC-USDT-SWAP')
        self.assertEqual(list(df.columns), ['ts', 'symbol', 'fundingRate', 'realizedRate'])
        self.assertEqual(len(df), 2)
        # 升序 + 用 real_funding_rate(实际结算)，不是 funding_rate(预测)
        self.assertEqual(df['ts'].tolist(), [1673769600000, 1673798400000])
        self.assertAlmostEqual(df['fundingRate'].iloc[0], -0.0008055)
        self.assertAlmostEqual(df['fundingRate'].iloc[1], 0.0004659)
        self.assertEqual(df['symbol'].iloc[0], 'BTC-USDT-SWAP')

    def test_empty_csv_returns_schema_only_frame(self):
        zb = _make_swaprate_zip([])
        df = H._parse_swaprate_zip(zb, 'BTC-USDT-SWAP')
        self.assertEqual(list(df.columns), ['ts', 'symbol', 'fundingRate', 'realizedRate'])
        self.assertTrue(df.empty)


class TestMergeFunding(unittest.TestCase):
    def _df(self, pairs):
        return pd.DataFrame([{'ts': ts, 'symbol': 'X', 'fundingRate': fr, 'realizedRate': fr}
                             for ts, fr in pairs])

    def test_rest_wins_on_overlap_and_sorted(self):
        swap = self._df([(1000, 0.1), (2000, 0.2)])   # 早期源
        rest = self._df([(2000, 0.99), (3000, 0.3)])  # 近期源(权威)
        out = P._merge_funding(swap, rest)
        self.assertEqual(out['ts'].tolist(), [1000, 2000, 3000])
        # ts=2000 重叠 → 取 REST 的值
        self.assertAlmostEqual(float(out.loc[out['ts'] == 2000, 'fundingRate'].iloc[0]), 0.99)

    def test_handles_none_and_empty_sources(self):
        rest = self._df([(3000, 0.3)])
        self.assertEqual(P._merge_funding(None, rest)['ts'].tolist(), [3000])
        self.assertEqual(P._merge_funding(rest, None)['ts'].tolist(), [3000])
        out = P._merge_funding(None, None)
        self.assertTrue(out.empty)
        self.assertEqual(list(out.columns), ['ts', 'symbol', 'fundingRate', 'realizedRate'])


class TestFundingMissing(unittest.TestCase):
    def _bars(self):
        # 持仓窗口：UTC 2026-06-01 00:00 起 12 根 1H
        t = pd.date_range('2026-06-01 00:00:00', periods=12, freq='1H')
        return pd.DataFrame({'candle_begin_time': t, 'close': 1.0})

    def _funding_at(self, hour_utc):
        ts = int(pd.Timestamp('2026-06-01 %02d:00:00' % hour_utc).timestamp() * 1000)
        return pd.DataFrame({'ts': [ts], 'symbol': ['X'], 'fundingRate': [0.0005], 'realizedRate': [0.0005]})

    def test_none_funding_is_missing(self):
        self.assertTrue(BT._funding_missing(None, self._bars()))

    def test_empty_funding_is_missing(self):
        empty = pd.DataFrame(columns=['ts', 'symbol', 'fundingRate', 'realizedRate'])
        self.assertTrue(BT._funding_missing(empty, self._bars()))

    def test_funding_inside_window_is_not_missing(self):
        self.assertFalse(BT._funding_missing(self._funding_at(8), self._bars()))  # 08:00 在窗口内

    def test_funding_only_outside_window_is_missing(self):
        self.assertTrue(BT._funding_missing(self._funding_at(20), self._bars()))  # 20:00 在窗口外


if __name__ == '__main__':
    unittest.main()
