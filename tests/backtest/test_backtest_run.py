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
    sub = holding_bars(series['AAA/USDT:USDT'], pd.Timestamp('2024-01-05 00:00:00'), '12H', 8)
    # 12H 窗口（UTC+8 对齐）应有约 12 根 1h bar
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
                      pd.Timestamp('2024-01-11 00:00:00'), _strategy(), FACTORS, 8,
                      timeframe='1h')
    # 至少跑出结果行，列齐全
    assert set(['run_time', 'offset', 'symbol', 'pnl_ratio', 'exit_reason',
                'grid_num', 'hold_bars']).issubset(df.columns)
    if not df.empty:
        assert df['pnl_ratio'].notna().all()
