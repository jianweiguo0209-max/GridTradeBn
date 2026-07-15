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


def test_filter_tasks_symbol_lock_semantics():
    # 镜像实盘 SymbolLockGate：同币 12h 锁窗内再选中 → 剔除且不递补；跨币不互扰；顺序保持。
    import pandas as pd
    from gridtrade.backtest.backtest_run import filter_tasks_symbol_lock
    def t(rt, sym):
        return (pd.Timestamp(rt), 0, sym, 1.0, {}, None, None)
    tasks = [t('2026-01-01 00:00', 'A'),
             t('2026-01-01 05:00', 'A'),    # A 锁窗内(5h) → 剔
             t('2026-01-01 05:00', 'B'),    # 异币不受 A 锁影响 → 留
             t('2026-01-01 12:00', 'A'),    # 恰好 12h 边界=锁释放 → 留（实盘轮换同刻关旧开新）
             t('2026-01-01 16:00', 'B'),    # B 锁窗内(11h) → 剔
             t('2026-01-01 23:00', 'A')]    # A 第二格锁窗内(11h) → 剔
    kept, n_rejected = filter_tasks_symbol_lock(tasks, period='12H')
    assert [(str(x[0]), x[2]) for x in kept] == [
        ('2026-01-01 00:00:00', 'A'), ('2026-01-01 05:00:00', 'B'),
        ('2026-01-01 12:00:00', 'A')]
    assert n_rejected == 3
    assert kept == [tasks[0], tasks[2], tasks[3]]   # 原对象、原顺序（零改写）


def test_run_backtest_symbol_lock_differential(tmp_path):
    # 差分：lock on 的网格集是 off 的真子集（只剔不增；默认 off 行为不变）。
    import pandas as pd
    from gridtrade.backtest.backtest_run import run_backtest
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10 00:00:00'), pd.Timestamp('2024-01-11 00:00:00')
    df_off = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h')
    df_on = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h',
                         symbol_lock=True)
    assert len(df_off) > len(df_on) > 0              # 每小时同币重选场景下必有剔除
    key = lambda d: set(zip(d['run_time'].astype(str), d['symbol']))
    assert key(df_on) <= key(df_off)                 # 真子集：只剔不增


def test_weights_from_env_override(monkeypatch):
    from gridtrade.backtest.backtest_run import _weights_from_env
    sc0 = {'weight_list': [1, 1, 1], 'period': '12H'}
    fac0 = {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True}
    # 无 env → 原样
    monkeypatch.delenv('BT_WEIGHTS', raising=False)
    monkeypatch.delenv('BT_SGCZ_DESC', raising=False)
    assert _weights_from_env(sc0, fac0) == (sc0, fac0)
    # BT_WEIGHTS 覆盖权重
    monkeypatch.setenv('BT_WEIGHTS', '1,0,2')
    sc, fac = _weights_from_env(sc0, fac0)
    assert sc['weight_list'] == [1.0, 0.0, 2.0] and sc0['weight_list'] == [1, 1, 1]  # 不改原
    # BT_SGCZ_DESC 翻转方向
    monkeypatch.setenv('BT_SGCZ_DESC', '1')
    sc, fac = _weights_from_env(sc0, fac0)
    assert fac['Sgcz_5'] is False and fac0['Sgcz_5'] is True


def test_run_backtest_shock_brake_wiring(tmp_path):
    """shock_brake 接线差分(spec 2026-07-08 回测同步):None=逐位基线;
    极低 thr(全程 fired)=全 rt 被拦→零格;正常 thr 下 blocked rt 无格。"""
    from gridtrade.backtest.backtest_run import run_backtest
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10 00:00:00'), pd.Timestamp('2024-01-11 00:00:00')
    base = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h')
    off = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h',
                       shock_brake=None)
    pd.testing.assert_frame_equal(base.reset_index(drop=True), off.reset_index(drop=True))

    allblk = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h',
                          shock_brake=(4, 1e-9, 6))       # thr≈0 → 全程 fired → 全拦
    assert len(allblk) == 0

    brk = run_backtest(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h',
                       shock_brake=(4, 0.04, 2))
    from gridtrade.backtest.shock_replay import blocked_rts
    blocked = blocked_rts(cache, syms, ws, we, '1h', 4, 0.04, 2)
    assert not set(pd.to_datetime(brk['run_time'])) & blocked   # 被拦 rt 上无格
    assert len(brk) <= len(base)


def test_default_fee_rates_binance_vip0():
    import inspect
    from gridtrade.backtest.backtest_run import simulate_tasks, run_backtest
    for fn in (simulate_tasks, run_backtest):
        sig = inspect.signature(fn)
        assert sig.parameters['fee_rate'].default == 0.0002
        assert sig.parameters['taker_rate'].default == 0.0005


def test_exclude_non_coin_drops_tradfi_keeps_delisted_coin():
    from gridtrade.backtest.backtest_run import exclude_non_coin
    from gridtrade.exchanges.binance import BinanceAdapter
    from tests.exchanges.test_binance_adapter import FakeBinanceClient
    c = FakeBinanceClient()
    c.markets = {
        'BTC/USDT:USDT': {'symbol': 'BTC/USDT:USDT', 'swap': True, 'settle': 'USDT',
                          'info': {'underlyingType': 'COIN'}},
        'SOXL/USDT:USDT': {'symbol': 'SOXL/USDT:USDT', 'swap': True, 'settle': 'USDT',
                           'info': {'underlyingType': 'EQUITY'}},
        'XAU/USDT:USDT': {'symbol': 'XAU/USDT:USDT', 'swap': True, 'settle': 'USDT',
                          'info': {'underlyingType': 'COMMODITY'}},
        'BTC/USDC:USDC': {'symbol': 'BTC/USDC:USDC', 'swap': True, 'settle': 'USDC',
                          'info': {'underlyingType': 'COIN'}},   # 非本结算币,不算入 non_coin
    }
    a = BinanceAdapter(c)
    # 归档含:现存 COIN(BTC)、现存 TradFi(SOXL/XAU)、已退市 COIN(FOO 不在当前 exchangeInfo)
    archive = {'BTC/USDT:USDT', 'SOXL/USDT:USDT', 'XAU/USDT:USDT', 'FOO/USDT:USDT'}
    kept, removed = exclude_non_coin(archive, a)
    assert kept == ['BTC/USDT:USDT', 'FOO/USDT:USDT']   # TradFi 剔除;退市 COIN 保留(无幸存者偏差)
    assert removed == 2                                  # SOXL + XAU
