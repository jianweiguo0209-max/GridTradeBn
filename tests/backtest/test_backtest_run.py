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


def _bars_qv(sym, qv, seed):
    import numpy as np
    from gridtrade.exchanges.base import CANDLE_COLS
    t = pd.date_range('2024-01-01', periods=300, freq='1H')
    close = 100.0 * np.exp(np.cumsum(np.random.RandomState(seed).normal(0, 0.01, 300)))
    open_ = np.concatenate([[100.0], close[:-1]])
    return pd.DataFrame({'symbol': sym, 'candle_begin_time': t,
                         'open': open_, 'high': np.maximum(open_, close) * 1.001,
                         'low': np.minimum(open_, close) * 0.999, 'close': close,
                         'vol': 1.0, 'volCcy': 1.0, 'quote_volume': float(qv)})[CANDLE_COLS]


def _seed_qv_cache(tmp_path, coins):
    from gridtrade.backtest.cache import ParquetCache
    cache = ParquetCache(str(tmp_path))
    for i, (sym, qv) in enumerate(coins):
        df = _bars_qv(sym, qv, seed=i + 1)
        for day, g in df.groupby(df['candle_begin_time'].dt.strftime('%Y-%m-%d')):
            cache.write('1h', sym, day, g.reset_index(drop=True))
    return cache


def test_run_backtest_min_quote_volume_filters(tmp_path):
    from gridtrade.backtest.backtest_run import run_backtest
    # 三币：RICH/RICH2 quote_volume 大（不同 seed → 有真实横截面），POOR 极小。
    # 地板剔 POOR 后仍剩 2 个候选，绕开 select_grid_coin 单候选时 55% 分位退化的边界。
    coins = [('RICH/USDC:USDC', 1e5), ('RICH2/USDC:USDC', 1e5), ('POOR/USDC:USDC', 1.0)]
    cache = _seed_qv_cache(tmp_path, coins)
    syms = [c[0] for c in coins]
    # 门槛=1e6：RICH/RICH2 24h 和 = 24*1e5=2.4e6 过；POOR=24*1=24 剔
    df = run_backtest(cache, syms, pd.Timestamp('2024-01-10 00:00:00'),
                      pd.Timestamp('2024-01-11 00:00:00'), _strategy(), FACTORS,
                      timeframe='1h', min_quote_volume=1_000_000.0)
    assert len(df) > 0                                       # 地板放行的高量币仍能入选
    assert (df['symbol'] == 'POOR/USDC:USDC').sum() == 0     # POOR 被地板剔、从不入选


def test_run_backtest_floor_excludes_all_returns_schema(tmp_path):
    # 门槛剔光所有币 → 空选择 → 返回带 schema 的空 DataFrame（下游取列不 KeyError）。
    from gridtrade.backtest.backtest_run import run_backtest, _RESULT_COLS
    coins = [('POOR/USDC:USDC', 1.0), ('POOR2/USDC:USDC', 2.0)]
    cache = _seed_qv_cache(tmp_path, coins)
    syms = [c[0] for c in coins]
    df = run_backtest(cache, syms, pd.Timestamp('2024-01-10 00:00:00'),
                      pd.Timestamp('2024-01-11 00:00:00'), _strategy(), FACTORS,
                      timeframe='1h', min_quote_volume=1_000_000.0)
    assert len(df) == 0                                      # 全被地板剔、无网格
    assert list(df.columns) == _RESULT_COLS                 # 空表仍带完整列
    assert (df['symbol'] == 'POOR/USDC:USDC').sum() == 0     # 取列不 KeyError
