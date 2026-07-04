"""并行(workers>1) 必须与串行(workers=1) 逐位一致 + funding 预切片不改结果。"""
import pandas as pd

from tests.backtest.test_selection_replay import _seed_cache, FACTORS
from tests.backtest.test_backtest_run import _strategy


def _run(cache, syms, workers):
    from gridtrade.backtest.backtest_run import run_backtest
    return run_backtest(cache, syms, pd.Timestamp('2024-01-10 00:00:00'),
                        pd.Timestamp('2024-01-11 00:00:00'), _strategy(), FACTORS,
                        timeframe='1h', workers=workers)


def test_parallel_matches_serial(tmp_path):
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    s = _run(cache, syms, workers=1).sort_values(['run_time', 'offset']).reset_index(drop=True)
    p = _run(cache, syms, workers=4).sort_values(['run_time', 'offset']).reset_index(drop=True)
    assert len(s) == len(p) and len(s) > 0
    pd.testing.assert_frame_equal(s, p, check_exact=False, rtol=1e-9)


def test_select_grids_parallel_matches_serial(tmp_path, monkeypatch):
    monkeypatch.setenv('BT_SELECT_CACHE', 'off')          # 隔离缓存（Task 6 后仍只测并行）
    from gridtrade.backtest.backtest_run import select_grids
    from tests.backtest.test_backtest_run import _strategy
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-09 00:00:00'), pd.Timestamp('2024-01-12 00:00:00')
    g1 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h', workers=1)
    g3 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h', workers=3)
    key = lambda gs: [(str(rt), int(off), row['symbol'], round(float(row['close']), 8))
                      for rt, off, row in gs]
    assert len(g1) > 0
    assert key(g1) == key(g3)
