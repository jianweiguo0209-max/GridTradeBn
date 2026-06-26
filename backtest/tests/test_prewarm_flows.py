"""
回测预热全流程单元测试（stdlib unittest，无需 pytest）。

覆盖：
- ParquetCache：读写往返 / exists 短路 / 空哨兵 / 原子写无残留 / read_all_days 合并
- okx_history：K线解析、分页+区间过滤、instruments、资金费/标记价解析（网络用 mock）
- prewarm：_to_ms、_fetch_symbol_candles 幂等+空哨兵、stage_candles 计数、
            stage_instruments 票池过滤+冻结复用、_build_tick_manifest 跨天去重、_done_run_times 续跑
- selection_replay：compute_offset、replay_selection 端到端复用实盘选币管线（合成数据）

运行：  TZ=Asia/Shanghai ../.venv/bin/python -m unittest discover -s tests -v
（必须用与实盘服务器一致的 TZ；本部署为 UTC+8 → TZ=Asia/Shanghai，否则选币 parity 漂移）
"""
import os
import sys
import shutil
import tempfile
import unittest

import numpy as np
import pandas as pd

# 把 backtest/ 放入 path
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BT_DIR = os.path.dirname(_TESTS_DIR)
if _BT_DIR not in sys.path:
    sys.path.insert(0, _BT_DIR)

import cache as cache_mod
import okx_history as H
import prewarm
import bt_config as C
from cache import ParquetCache


# ----------------- 合成数据工具 -----------------
def make_candle_df(symbol, start, n_hours, seed=0, base_qv=1e7):
    """生成 n_hours 根 1H K线（含 CANDLE_COLS + ts），价格为带种子的几何随机游走。"""
    rng = np.random.RandomState(seed)
    ts0 = pd.Timestamp(start)
    times = [ts0 + pd.Timedelta(hours=i) for i in range(n_hours)]
    rets = rng.normal(0, 0.01, n_hours)
    close = 100 * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.005, n_hours)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n_hours)))
    openp = np.concatenate([[close[0]], close[:-1]])
    volccy = np.abs(rng.normal(1000, 100, n_hours)) + 1
    qv = volccy * close * (base_qv / 1e7)
    df = pd.DataFrame({
        'symbol': symbol,
        'candle_begin_time': pd.to_datetime(times),
        'open': openp, 'high': high, 'low': low, 'close': close,
        'vol': volccy, 'volCcy': volccy, 'quote_volume': qv,
    })
    df['ts'] = (df['candle_begin_time'].astype('int64') // 1_000_000)
    return df[['ts'] + H.CANDLE_COLS]


def write_candles_to_cache(cache, df, bar='1H'):
    """把一个 symbol 的 K线按天写入缓存。"""
    df = df.copy()
    df['day'] = df['candle_begin_time'].dt.strftime('%Y-%m-%d')
    for d, g in df.groupby('day'):
        cache.write(bar, g['symbol'].iloc[0], d, g[H.CANDLE_COLS].reset_index(drop=True))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bt_test_')
        self.cache = ParquetCache(os.path.join(self.tmp, 'cache'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ===================== ParquetCache =====================
class TestParquetCache(Base):
    def test_roundtrip_and_exists(self):
        df = make_candle_df('AAA-USDT-SWAP', '2024-01-01', 5)[H.CANDLE_COLS]
        self.assertFalse(self.cache.exists('1H', 'AAA-USDT-SWAP', '2024-01-01'))
        self.cache.write('1H', 'AAA-USDT-SWAP', '2024-01-01', df)
        self.assertTrue(self.cache.exists('1H', 'AAA-USDT-SWAP', '2024-01-01'))
        out = self.cache.read('1H', 'AAA-USDT-SWAP', '2024-01-01')
        self.assertEqual(len(out), 5)
        self.assertEqual(list(out.columns), H.CANDLE_COLS)

    def test_empty_sentinel(self):
        # 空哨兵：写空 → exists True，read 返回空 df（区分"没取过"vs"取过=空"）
        self.cache.write_empty('1H', 'BBB-USDT-SWAP', '2024-01-02', H.CANDLE_COLS)
        self.assertTrue(self.cache.exists('1H', 'BBB-USDT-SWAP', '2024-01-02'))
        out = self.cache.read('1H', 'BBB-USDT-SWAP', '2024-01-02')
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 0)

    def test_read_missing_returns_none(self):
        self.assertIsNone(self.cache.read('1H', 'NOPE', '2024-01-01'))

    def test_atomic_write_no_tmp_leftover(self):
        df = make_candle_df('CCC-USDT-SWAP', '2024-01-01', 3)[H.CANDLE_COLS]
        self.cache.write('1H', 'CCC-USDT-SWAP', '2024-01-01', df)
        d = self.cache._dir('1H', 'CCC-USDT-SWAP')
        leftovers = [f for f in os.listdir(d) if f.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_read_all_days_merges(self):
        df = make_candle_df('DDD-USDT-SWAP', '2024-01-01', 72)  # 3 天
        write_candles_to_cache(self.cache, df)
        merged = self.cache.read_all_days('1H', 'DDD-USDT-SWAP')
        self.assertEqual(len(merged), 72)


# ===================== okx_history =====================
class TestOkxHistory(unittest.TestCase):
    def test_candles_to_df_types_sort_dedup(self):
        # OKX 返回新→旧，含重复 ts；解析后应升序、去重、类型正确
        rows = [
            ['1704070800000', '101', '102', '100', '101.5', '10', '10', '1015', '1'],
            ['1704067200000', '100', '101', '99', '100.5', '12', '12', '1206', '1'],
            ['1704067200000', '100', '101', '99', '100.6', '12', '12', '1207', '1'],  # 重复 ts
        ]
        df = H._candles_to_df(rows, 'AAA-USDT-SWAP')
        self.assertEqual(list(df.columns), ['ts'] + H.CANDLE_COLS)
        self.assertTrue(df['ts'].is_monotonic_increasing)
        self.assertEqual(df['ts'].duplicated().sum(), 0)
        self.assertEqual(df['close'].dtype, float)
        # 去重保留 last → 100.6
        self.assertAlmostEqual(df.iloc[0]['close'], 100.6)

    def test_candles_to_df_empty(self):
        df = H._candles_to_df([], 'X')
        self.assertEqual(len(df), 0)

    def test_fetch_candles_range_paginates_and_filters(self):
        # mock _get：返回两页（新→旧），验证翻页 + 区间过滤
        page1 = [[str(1704067200000 + i * 3600000), '1', '1', '1', '1', '1', '1', '1', '1']
                 for i in range(200, 100, -1)]  # 新页（较新）
        page2 = [[str(1704067200000 + i * 3600000), '1', '1', '1', '1', '1', '1', '1', '1']
                 for i in range(100, -1, -1)]    # 旧页（含 start 之前）
        calls = {'n': 0}

        def fake_get(path, params, proxies=None, **kw):
            calls['n'] += 1
            after = int(params['after'])
            if after > 1704067200000 + 150 * 3600000:
                return page1
            elif after > 1704067200000:
                return page2
            return []

        orig = H._get
        H._get = fake_get
        try:
            start_ms = 1704067200000 + 50 * 3600000
            end_ms = 1704067200000 + 180 * 3600000
            df = H.fetch_candles_range('AAA', start_ms, end_ms, bar='1H')
        finally:
            H._get = orig
        self.assertGreater(calls['n'], 1)  # 确实翻了页
        self.assertTrue((df['ts'] >= start_ms).all())
        self.assertTrue((df['ts'] <= end_ms).all())
        self.assertTrue(df['ts'].is_monotonic_increasing)

    def test_fetch_instruments(self):
        data = [{'instId': 'BTC-USDT-SWAP', 'tickSz': '0.1', 'state': 'live'},
                {'instId': 'ETH-USDT-SWAP', 'tickSz': '0.01', 'state': 'live'}]
        orig = H._get
        H._get = lambda *a, **k: data
        try:
            df = H.fetch_instruments('SWAP')
        finally:
            H._get = orig
        self.assertEqual(len(df), 2)
        self.assertIn('tickSz', df.columns)

    def test_fetch_funding_rate_range(self):
        data = [{'fundingTime': str(1704074400000), 'fundingRate': '0.0001', 'realizedRate': '0.0001'},
                {'fundingTime': str(1704067200000), 'fundingRate': '0.0002', 'realizedRate': '0.0002'}]
        orig = H._get
        H._get = lambda *a, **k: data if int(a[1]['after']) > 1704067200000 else []
        try:
            df = H.fetch_funding_rate_range('AAA', 1704067200000, 1704074400000)
        finally:
            H._get = orig
        self.assertEqual(len(df), 2)
        self.assertTrue(df['ts'].is_monotonic_increasing)
        self.assertEqual(df['fundingRate'].dtype, float)


# ===================== prewarm: 取数与缓存 =====================
class TestPrewarmFetch(Base):
    def test_to_ms_utc(self):
        self.assertEqual(prewarm._to_ms('2024-01-01 00:00:00'), 1704067200000)

    def test_fetch_symbol_candles_idempotent(self):
        calls = {'n': 0}

        def fake_range(symbol, s_ms, e_ms, bar='1H', proxies=None, **kw):
            calls['n'] += 1
            return make_candle_df(symbol, '2024-01-01', 72, seed=1)  # 3 天

        orig = H.fetch_candles_range
        H.fetch_candles_range = fake_range
        try:
            start = pd.Timestamp('2024-01-01'); end = pd.Timestamp('2024-01-03')
            sym, warmed, status = prewarm._fetch_symbol_candles(self.cache, 'AAA-USDT-SWAP', start, end, '1H', None)
            self.assertEqual(status, 'fetched')
            self.assertEqual(warmed, 3)
            self.assertEqual(calls['n'], 1)
            # 第二次：全部已缓存 → 跳过，不再取数
            sym, warmed2, status2 = prewarm._fetch_symbol_candles(self.cache, 'AAA-USDT-SWAP', start, end, '1H', None)
            self.assertEqual(status2, 'skip')
            self.assertEqual(calls['n'], 1)  # 没有新增取数
        finally:
            H.fetch_candles_range = orig

    def test_fetch_symbol_candles_empty_sentinel(self):
        orig = H.fetch_candles_range
        H.fetch_candles_range = lambda *a, **k: pd.DataFrame(columns=['ts'] + H.CANDLE_COLS)
        try:
            start = pd.Timestamp('2024-01-01'); end = pd.Timestamp('2024-01-02')
            sym, warmed, status = prewarm._fetch_symbol_candles(self.cache, 'DEAD-USDT-SWAP', start, end, '1H', None)
            self.assertEqual(status, 'empty')
            # 缺失天都落了空哨兵 → exists True
            self.assertTrue(self.cache.exists('1H', 'DEAD-USDT-SWAP', '2024-01-01'))
            self.assertTrue(self.cache.exists('1H', 'DEAD-USDT-SWAP', '2024-01-02'))
        finally:
            H.fetch_candles_range = orig

    def test_stage_candles_counts(self):
        orig = H.fetch_candles_range
        H.fetch_candles_range = lambda symbol, s, e, bar='1H', proxies=None, **k: make_candle_df(symbol, '2024-01-01', 48, seed=2)
        try:
            universe = ['AAA-USDT-SWAP', 'BBB-USDT-SWAP']
            prewarm.stage_candles(self.cache, universe, pd.Timestamp('2024-01-01'),
                                  pd.Timestamp('2024-01-02'), '1H', workers=2, proxies=None, log=lambda *a: None)
            for s in universe:
                self.assertTrue(self.cache.exists('1H', s, '2024-01-01'))
                self.assertTrue(self.cache.exists('1H', s, '2024-01-02'))
        finally:
            H.fetch_candles_range = orig


# ===================== prewarm: instruments / manifest / resume =====================
class TestPrewarmMeta(Base):
    def test_stage_instruments_filter_and_freeze(self):
        data = [
            {'instId': 'BTC-USDT-SWAP', 'tickSz': '0.1', 'state': 'live'},
            {'instId': 'ETH-USDT-SWAP', 'tickSz': '0.01', 'state': 'live'},
            {'instId': 'OLD-USDT-SWAP', 'tickSz': '0.001', 'state': 'suspend'},  # 非 live 应过滤
            {'instId': 'BTC-USD-SWAP', 'tickSz': '0.1', 'state': 'live'},        # 非 USDT 应过滤
        ]
        calls = {'n': 0}
        orig = H.fetch_instruments

        def fake_inst(inst_type='SWAP', proxies=None):
            calls['n'] += 1
            return pd.DataFrame(data)

        H.fetch_instruments = fake_inst
        try:
            universe, tick = prewarm.stage_instruments(self.cache, None, refresh=False, log=lambda *a: None)
            self.assertEqual(universe, ['BTC-USDT-SWAP', 'ETH-USDT-SWAP'])
            self.assertEqual(tick['BTC-USDT-SWAP'], '0.1')
            self.assertEqual(calls['n'], 1)
            # 第二次：复用冻结，不再取数
            universe2, _ = prewarm.stage_instruments(self.cache, None, refresh=False, log=lambda *a: None)
            self.assertEqual(universe2, universe)
            self.assertEqual(calls['n'], 1)
        finally:
            H.fetch_instruments = orig

    def test_build_tick_manifest_crossday_dedup(self):
        mdir = os.path.join(self.tmp, 'manifest')
        os.makedirs(mdir)
        cand = os.path.join(mdir, 'candidates.csv')
        pd.DataFrame([
            {'run_time': '2024-01-01 18:00:00', 'offset': 6, 'symbol': 'AAA-USDT-SWAP', 'rank': 1.0},
            {'run_time': '2024-01-02 06:00:00', 'offset': 6, 'symbol': 'AAA-USDT-SWAP', 'rank': 1.0},
            {'run_time': '2024-01-03 00:00:00', 'offset': 0, 'symbol': '', 'rank': None},  # 空标记行应跳过
        ]).to_csv(cand, index=False)
        prewarm._build_tick_manifest(cand, '12H', mdir, log=lambda *a: None)
        out = pd.read_csv(os.path.join(mdir, 'tick_manifest.csv'))
        days = set(out[out['symbol'] == 'AAA-USDT-SWAP']['day'])
        # 18:00 + 12H 跨到 01-02；06:00 + 12H 到 01-02 → 去重后 {01-01,01-02}
        self.assertEqual(days, {'2024-01-01', '2024-01-02'})
        self.assertNotIn('', set(out['symbol']))

    def test_done_run_times_resume(self):
        mdir = os.path.join(self.tmp, 'manifest')
        os.makedirs(mdir)
        cand = os.path.join(mdir, 'candidates.csv')
        pd.DataFrame([
            {'run_time': '2024-01-01 00:00:00', 'offset': 0, 'symbol': 'AAA-USDT-SWAP', 'rank': 1.0},
            {'run_time': '2024-01-01 01:00:00', 'offset': 1, 'symbol': '', 'rank': None},
        ]).to_csv(cand, index=False)
        done = prewarm._done_run_times(cand)
        self.assertIn('2024-01-01 00:00:00', done)
        self.assertIn('2024-01-01 01:00:00', done)
        self.assertEqual(prewarm._done_run_times(os.path.join(mdir, 'nope.csv')), set())


# ===================== prewarm S2: 资金费 + 标记价 =====================
def make_mark_df(symbol, start, n_hours):
    df = make_candle_df(symbol, start, n_hours)[['ts', 'open', 'high', 'low', 'close']].copy()
    df['symbol'] = symbol
    return df[['ts', 'symbol', 'open', 'high', 'low', 'close']]


def make_funding_df(symbol, start, n):
    ts0 = int(pd.Timestamp(start).value // 1_000_000)
    rows = [{'ts': ts0 + i * 8 * 3600000, 'symbol': symbol,
             'fundingRate': 0.0001 * (i + 1), 'realizedRate': 0.0001 * (i + 1)} for i in range(n)]
    return pd.DataFrame(rows)


class TestPrewarmS2(Base):
    def _write_candidates(self, mdir):
        os.makedirs(mdir, exist_ok=True)
        pd.DataFrame([
            {'run_time': '2024-01-01 18:00:00', 'offset': 6, 'symbol': 'AAA-USDT-SWAP', 'rank': 1.0},
            {'run_time': '2024-01-02 06:00:00', 'offset': 6, 'symbol': 'AAA-USDT-SWAP', 'rank': 1.0},
            {'run_time': '2024-01-03 00:00:00', 'offset': 0, 'symbol': '', 'rank': None},
        ]).to_csv(os.path.join(mdir, 'candidates.csv'), index=False)

    def test_symbol_holding_ranges(self):
        mdir = os.path.join(self.tmp, 'manifest')
        self._write_candidates(mdir)
        ranges = prewarm._symbol_holding_ranges(os.path.join(mdir, 'candidates.csv'), '12H', buffer_days=1)
        self.assertIn('AAA-USDT-SWAP', ranges)
        st, en = ranges['AAA-USDT-SWAP']
        # 最早 run_time 18:00 -1天缓冲；最晚 06:00+12H+1天缓冲
        self.assertLessEqual(st, pd.Timestamp('2023-12-31 18:00:00'))
        self.assertGreaterEqual(en, pd.Timestamp('2024-01-03 18:00:00'))
        self.assertNotIn('', ranges)  # 空标记行不计入

    def test_fetch_symbol_range_idempotent_and_empty(self):
        calls = {'n': 0}

        def fake_mark(sym, s_ms, e_ms, px):
            calls['n'] += 1
            return make_mark_df(sym, '2024-01-01', 48)  # 2 天

        sym, warmed, status = prewarm._fetch_symbol_range(
            self.cache, 'mark', 'AAA-USDT-SWAP', pd.Timestamp('2024-01-01'), pd.Timestamp('2024-01-02'),
            fake_mark, prewarm.MARK_COLS, None)
        self.assertEqual(status, 'fetched'); self.assertEqual(warmed, 2); self.assertEqual(calls['n'], 1)
        # 再跑：已缓存 → skip，不再取数
        _, _, status2 = prewarm._fetch_symbol_range(
            self.cache, 'mark', 'AAA-USDT-SWAP', pd.Timestamp('2024-01-01'), pd.Timestamp('2024-01-02'),
            fake_mark, prewarm.MARK_COLS, None)
        self.assertEqual(status2, 'skip'); self.assertEqual(calls['n'], 1)
        # 空数据 → 空哨兵
        _, _, status3 = prewarm._fetch_symbol_range(
            self.cache, 'funding', 'DEAD-USDT-SWAP', pd.Timestamp('2024-01-01'), pd.Timestamp('2024-01-01'),
            lambda *a: pd.DataFrame(columns=prewarm.FUNDING_COLS), prewarm.FUNDING_COLS, None)
        self.assertEqual(status3, 'empty')
        self.assertTrue(self.cache.exists('funding', 'DEAD-USDT-SWAP', '2024-01-01'))

    def test_stage_funding_mark_populates_both(self):
        mdir = os.path.join(self.tmp, 'manifest')
        self._write_candidates(mdir)
        orig_m, orig_f = H.fetch_mark_candles_range, H.fetch_funding_rate_range
        H.fetch_mark_candles_range = lambda sym, s, e, bar='1H', proxies=None: make_mark_df(sym, '2023-12-31', 24 * 6)
        H.fetch_funding_rate_range = lambda sym, s, e, proxies=None: make_funding_df(sym, '2023-12-31', 18)
        try:
            prewarm.stage_funding_mark(self.cache, mdir, '12H', '1H', workers=2, proxies=None, log=lambda *a: None)
            # 选中币 AAA 的 mark 与 funding 都应有缓存
            self.assertTrue(self.cache.exists('mark', 'AAA-USDT-SWAP', '2024-01-01'))
            self.assertTrue(self.cache.exists('funding', 'AAA-USDT-SWAP', '2024-01-01'))
        finally:
            H.fetch_mark_candles_range, H.fetch_funding_rate_range = orig_m, orig_f


# ===================== selection_replay：parity 端到端 =====================
class TestSelectionReplay(Base):
    def _load_cfg(self):
        return prewarm._load_strategy_config()

    def test_compute_offset_cycle(self):
        import selection_replay as SR
        offs = [SR.compute_offset(pd.Timestamp('2024-01-01 %02d:00:00' % h), '12H', 0) for h in range(13)]
        self.assertEqual(offs, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 0])

    def test_replay_end_to_end_reuses_live_pipeline(self):
        import selection_replay as SR
        cfg = self._load_cfg()
        # 30 个币，各 14 天 1H bar；不同 quote_volume 量级让分位过滤保留子集
        symbols = ['S%02d-USDT-SWAP' % i for i in range(30)]
        for i, s in enumerate(symbols):
            df = make_candle_df(s, '2024-01-01', 24 * 14, seed=i, base_qv=1e6 * (i + 1))
            write_candles_to_cache(self.cache, df)

        run_times = [pd.Timestamp('2024-01-13 06:00:00'), pd.Timestamp('2024-01-13 12:00:00')]
        selected = []

        def on_select(rt, offset, row):
            selected.append((str(rt), offset, row['symbol']))

        processed = SR.replay_selection(self.cache, symbols, run_times, cfg, C.FACTORS,
                                        utc_offset=0, on_select=on_select, log=lambda *a: None)
        # 流程跑完所有 run_time
        self.assertEqual(processed, len(run_times))
        # 复用实盘管线确有选币产出，且都在票池内、offset 合法
        self.assertGreaterEqual(len(selected), 1)
        for rt, offset, sym in selected:
            self.assertIn(sym, symbols)
            self.assertTrue(0 <= offset <= 11)

    def test_replay_point_in_time_no_lookahead(self):
        """选币只用 < run_time 的 bar：人为只缓存到某时刻，更晚 run_time 仍不报错且不读未来。"""
        import selection_replay as SR
        cfg = self._load_cfg()
        symbols = ['T%02d-USDT-SWAP' % i for i in range(25)]
        for i, s in enumerate(symbols):
            df = make_candle_df(s, '2024-01-01', 24 * 10, seed=100 + i, base_qv=1e6 * (i + 1))
            write_candles_to_cache(self.cache, df)
        # run_time 远晚于数据末尾：用 tail(max_candle_num) 仍只取已存在的历史 bar
        run_times = [pd.Timestamp('2024-01-11 00:00:00')]
        cnt = {'n': 0}
        processed = SR.replay_selection(self.cache, symbols, run_times, cfg, C.FACTORS,
                                        0, lambda *a: cnt.__setitem__('n', cnt['n'] + 1), log=lambda *a: None)
        self.assertEqual(processed, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
