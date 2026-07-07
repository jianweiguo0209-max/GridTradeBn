import os

import pandas as pd

from tests.backtest.test_selection_replay import _seed_cache, _bars, STRAT, FACTORS


def test_compute_key_deterministic_and_sensitive(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    k1, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    k2, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert k1 == k2                                              # 确定性
    k3, _ = SC.compute_key(cache, syms, ws, we, '1h', 1e6, (), STRAT, FACTORS)
    assert k3 != k1                                             # min_quote_volume 改 → 换 key
    k4, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (),
                           dict(STRAT, choose_symbols=2), FACTORS)
    assert k4 != k1                                             # choose_symbols 改 → 换 key


def test_save_load_roundtrip(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert SC.load(cache, key, params) is None                 # 未写 → MISS
    grids = [(pd.Timestamp('2024-01-10'), 0, pd.Series({'symbol': 'AAA/USDT:USDT', 'close': 1.0}))]
    SC.save(cache, key, params, grids)
    got = SC.load(cache, key, params)
    assert got is not None and len(got) == 1
    assert got[0][2]['symbol'] == 'AAA/USDT:USDT' and got[0][1] == 0


def test_load_rejects_param_mismatch(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    SC.save(cache, key, params, [(ws, 0, pd.Series({'symbol': 'X', 'close': 1.0}))])
    tampered = dict(params, choose_symbols=999)                # params 不一致 → 拒（防碰撞）
    assert SC.load(cache, key, tampered) is None


def test_fingerprint_scoped_to_window(tmp_path):
    """指纹只覆盖 [window_start−回看, window_end]：追加 window_end 之后的天不翻 key
    （修复'例行追加近期数据打翻历史窗缓存'）；范围内新增天仍翻 key（正确失效）。"""
    from gridtrade.backtest import select_cache as SC
    from gridtrade.backtest.cache import ParquetCache
    syms = ['AAA/USDT:USDT']
    cache = ParquetCache(str(tmp_path))
    df = _bars(syms[0], n=192, start='2024-01-01')          # 192h = 01-01..01-08，都在回看范围内
    for day, g in df.groupby(df['candle_begin_time'].dt.strftime('%Y-%m-%d')):
        cache.write('1h', syms[0], day, g.reset_index(drop=True))
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-12')
    k0, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)

    # (1) 追加 window_end 之后的天 → 范围外 → key 不变（核心修复）
    cache.write('1h', syms[0], '2024-01-20', _bars(syms[0], n=24, start='2024-01-20'))
    k_future, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert k_future == k0, '追加 window_end 之后的天不应翻 key'

    # (2) 追加窗口范围内的天（≤ window_end 且 ≥ 回看下界）→ key 变（正确失效）
    cache.write('1h', syms[0], '2024-01-11', _bars(syms[0], n=24, start='2024-01-11'))
    k_inrange, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert k_inrange != k0, '范围内新增天应翻 key'


def test_enabled_env(monkeypatch):
    from gridtrade.backtest import select_cache as SC
    monkeypatch.delenv('BT_SELECT_CACHE', raising=False)
    assert SC.enabled() is True
    monkeypatch.setenv('BT_SELECT_CACHE', 'off')
    assert SC.enabled() is False


def test_select_grids_cache_hit_skips_recompute(tmp_path, monkeypatch):
    import gridtrade.backtest.selection_replay as SR
    from gridtrade.backtest.backtest_run import select_grids
    from tests.backtest.test_backtest_run import _strategy
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-09'), pd.Timestamp('2024-01-12')
    g1 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h')   # MISS 写
    assert len(g1) > 0

    def _boom(*a, **k):
        raise AssertionError('cache HIT 不应再调 replay_selection')
    monkeypatch.setattr(SR, 'replay_selection', _boom)
    g2 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h')   # HIT 读
    key = lambda gs: [(str(rt), int(off), row['symbol']) for rt, off, row in gs]
    assert key(g1) == key(g2)


def test_select_grids_cache_off_never_writes(tmp_path, monkeypatch):
    from gridtrade.backtest.backtest_run import select_grids
    from tests.backtest.test_backtest_run import _strategy
    monkeypatch.setenv('BT_SELECT_CACHE', 'off')
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h')
    assert not os.path.isdir(os.path.join(str(tmp_path), '_select_cache'))          # off → 不落盘


def test_select_grids_parallel_then_cache_hit(tmp_path):
    from gridtrade.backtest.backtest_run import select_grids
    from tests.backtest.test_backtest_run import _strategy
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-09'), pd.Timestamp('2024-01-12')
    g3 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h', workers=3)  # MISS 并行写
    g1 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h', workers=1)  # HIT 读
    key = lambda gs: [(str(rt), int(off), row['symbol'], round(float(row['close']), 8))
                      for rt, off, row in gs]
    assert len(g3) > 0 and key(g1) == key(g3)
