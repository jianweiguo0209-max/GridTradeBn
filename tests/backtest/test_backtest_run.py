import pandas as pd

from tests.backtest.test_selection_replay import _seed_cache, STRAT, FACTORS


def _strategy():
    return dict(STRAT, leverage=5, price_limit=[0.25, 0.25], stop_limit=0.01,
                grid_version=2,
                grid_v2_config={'atr_range_multiplier': 3, 'range_pct_min': 0.05,
                                'range_pct_max': 0.25, 'grid_spacing_atr_ratio': 0.5,
                                'grid_spacing_min': 0.003, 'grid_spacing_max': 0.02,
                                'grid_count_min': 25, 'grid_count_max': 149,
                                'stop_buffer_ratio': 0.01},
                stop_loss_config={'stop_loss': 0.034, 'trailing_k': 0.3,
                                  'trailing_floor': 0.00618, 'fundingRate_stop_loss': 0.0015})


def test_holding_bars_window(tmp_path):
    from gridtrade.backtest.backtest_run import holding_bars
    from gridtrade.backtest.selection_replay import load_full_series
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    series = load_full_series(cache, syms, '1h')
    sub = holding_bars(series['AAA/USDT:USDT'], pd.Timestamp('2024-01-05 00:00:00'), '12H')
    # 12H 窗口（纯 UTC 对齐）应有约 12 根 1h bar
    assert 1 <= len(sub) <= 13


def test_summarize_shape():
    from gridtrade.backtest.backtest_run import summarize
    df = pd.DataFrame({'run_time': pd.to_datetime(['2024-01-01', '2024-01-01']),
                       'offset': [0, 1], 'pnl_ratio': [0.02, -0.01],
                       'exit_reason': ['窗口结束', '固定止损']})
    s = summarize(df)
    assert s['n_grids'] == 2 and 0.0 <= s['win_rate'] <= 1.0
    assert 'portfolio_return' in s and 'exit_reasons' in s


def test_summarize_empty():
    from gridtrade.backtest.backtest_run import summarize
    assert summarize(pd.DataFrame())['n_grids'] == 0


def test_run_backtest_end_to_end(tmp_path):
    from gridtrade.backtest.backtest_run import run_backtest
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    df = run_backtest(cache, syms, pd.Timestamp('2024-01-10 00:00:00'),
                      pd.Timestamp('2024-01-11 00:00:00'), _strategy(), FACTORS,
                      timeframe='1h')
    assert set(['run_time', 'offset', 'symbol', 'pnl_ratio', 'exit_reason',
                'grid_num', 'hold_bars']).issubset(df.columns)
    assert len(df) > 0                                   # 端到端真的跑出网格（非空过）
    assert df['pnl_ratio'].notna().all()
    assert df['exit_reason'].map(lambda r: isinstance(r, str) and len(r) > 0).all()


def test_run_backtest_floor_and_blacklist_gate_selection(tmp_path):
    # 差分证明地板/黑名单真的接线：同 fixture，关=有网格，开到剔光=空。
    from gridtrade.backtest.backtest_run import run_backtest, _RESULT_COLS
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10 00:00:00'), pd.Timestamp('2024-01-11 00:00:00')
    base = dict(timeframe='1h')
    df0 = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, min_quote_volume=0.0, **base)
    assert len(df0) > 0                                   # 无地板/黑名单：选出网格（baseline）
    # 地板高到剔光所有币 → 空（若地板未穿到 replay_selection，会 == df0 非空 → 此断言失败）
    dfhi = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, min_quote_volume=1e12, **base)
    assert len(dfhi) == 0 and list(dfhi.columns) == _RESULT_COLS
    # 黑名单全禁 → 空（同理证明 blacklist 已穿线）
    dfbl = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, blacklist=tuple(syms), **base)
    assert len(dfbl) == 0


def test_select_grids_then_assemble_equals_build_grid_tasks(tmp_path):
    # _seed_cache 已在本文件顶部 import（from tests.backtest.test_selection_replay import _seed_cache, STRAT, FACTORS）
    from gridtrade.backtest.backtest_run import (build_grid_tasks, select_grids,
                                                 assemble_grid_tasks)
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10 00:00:00'), pd.Timestamp('2024-01-11 00:00:00')
    strat = _strategy()
    a = build_grid_tasks(cache, syms, ws, we, strat, FACTORS, timeframe='1h')
    grids = select_grids(cache, syms, ws, we, strat, FACTORS, timeframe='1h')
    b = assemble_grid_tasks(cache, grids, strat, timeframe='1h')
    # 选中集 == build 的组装集（按 (rt,sym) 比对）
    key = lambda tasks: sorted((str(t[0]), t[2]) for t in tasks)
    assert key(a) == key(b)
