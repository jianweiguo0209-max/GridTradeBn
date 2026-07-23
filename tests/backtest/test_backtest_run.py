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


PEPE_TIERS = [{'maxLeverage': 25, 'maxNotional': 5000.0},
              {'maxLeverage': 20, 'maxNotional': 10000.0},
              {'maxLeverage': 13, 'maxNotional': 50000.0},
              {'maxLeverage': 4, 'maxNotional': 1000000.0}]
KITE_TIERS = [{'maxLeverage': 5, 'maxNotional': 5000.0},
              {'maxLeverage': 4, 'maxNotional': 10000.0},
              {'maxLeverage': 1, 'maxNotional': 200000.0}]


def test_exclude_low_leverage_drops_low_bracket_keeps_delisted():
    # 回测同步实盘票池杠杆过滤(2026-07-18,用户定默认=实盘阈值10):pick_L<10 剔除;
    # 退市币不在当前档位表 → 保留(无幸存者偏差,与 exclude_non_coin 同语义)
    from gridtrade.backtest.backtest_run import exclude_low_leverage
    raw = {'PEPE/USDT:USDT': PEPE_TIERS, 'KITE/USDT:USDT': KITE_TIERS}
    kept, removed = exclude_low_leverage(
        ['PEPE/USDT:USDT', 'KITE/USDT:USDT', 'DEAD/USDT:USDT'],
        lambda: raw, notional=3400.0, gearing=3.4, min_lev=10.0)
    assert kept == ['DEAD/USDT:USDT', 'PEPE/USDT:USDT']   # PEPE pick_L=20 留;退市 DEAD 留
    assert removed == 1                                    # KITE pick_L=4 剔


def test_exclude_low_leverage_disabled_when_zero():
    from gridtrade.backtest.backtest_run import exclude_low_leverage
    kept, removed = exclude_low_leverage(['KITE/USDT:USDT'], lambda: {},
                                         notional=3400.0, gearing=3.4, min_lev=0.0)
    assert kept == ['KITE/USDT:USDT'] and removed == 0


def test_exclude_low_leverage_fail_loud_on_empty_or_error():
    # 回测无 MarginGate 兜底:档位取不到时静默跳过=静默背离实盘票池 → 宁可整跑失败
    # (沿 exclude_non_coin fail-loud 先例;BT_MIN_LEVERAGE=0 为显式停用出口)
    import pytest
    from gridtrade.backtest.backtest_run import exclude_low_leverage
    with pytest.raises(RuntimeError):
        exclude_low_leverage(['X/USDT:USDT'], lambda: {},
                             notional=3400.0, gearing=3.4, min_lev=10.0)

    def _boom():
        raise ValueError('auth required')
    with pytest.raises(RuntimeError):
        exclude_low_leverage(['X/USDT:USDT'], _boom,
                             notional=3400.0, gearing=3.4, min_lev=10.0)


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


def test_exclude_non_coin_raises_on_empty_markets():
    # 降级响应(load_markets 返回空 markets、未抛异常)不得静默 fail-open 为"保留全量归档"
    # (等于放行 TradFi)——必须 fail-loud,与实盘 fail-closed 同向(spec 2026-07-15 §4.3)。
    import pytest

    from gridtrade.backtest.backtest_run import exclude_non_coin
    from gridtrade.exchanges.binance import BinanceAdapter
    from tests.exchanges.test_binance_adapter import FakeBinanceClient
    c = FakeBinanceClient()
    c.markets = {}   # 模拟降级:load_markets 幂等返回空,不抛
    a = BinanceAdapter(c)
    with pytest.raises(RuntimeError):
        exclude_non_coin({'BTC/USDT:USDT'}, a)


class _UniClient:
    """resolve_bt_universe 契约桩：markets 含 COIN/非COIN,杠杆档含高/低杠杆。"""
    def __init__(self):
        self.markets = {
            'AAA/USDT:USDT': {'symbol': 'AAA/USDT:USDT', 'swap': True, 'settle': 'USDT',
                              'info': {'underlyingType': 'COIN'}},
            'TRAD/USDT:USDT': {'symbol': 'TRAD/USDT:USDT', 'swap': True, 'settle': 'USDT',
                               'info': {'underlyingType': 'EQUITY'}},   # TradFi → 剔
            'LOW/USDT:USDT': {'symbol': 'LOW/USDT:USDT', 'swap': True, 'settle': 'USDT',
                              'info': {'underlyingType': 'COIN'}},      # 低杠杆 → 剔
        }

    def load_markets(self):
        return self.markets

    def fetch_leverage_tiers(self, symbols=None, params=None):
        # 档位表:AAA 高杠杆(50x@任意名义), LOW 低杠杆(5x);退市 DEAD 不在表 → 保留
        def tier(lev):
            return [{'maxNotional': 1e12, 'maxLeverage': lev,
                     'info': {'initialLeverage': str(lev), 'notionalCap': '1000000000000'}}]
        # ⚠ tier 行形状以同文件 test_exclude_low_leverage_* 既有桩为准——若 exclude_low_leverage
        # 解析报 KeyError,逐字段对齐既有桩,勿改生产代码
        return {'AAA/USDT:USDT': tier(50), 'LOW/USDT:USDT': tier(5)}


def _uni_adapter():
    from gridtrade.exchanges.binance import BinanceAdapter
    return BinanceAdapter(_UniClient())


def test_resolve_bt_universe_applies_both_filters_keeps_delisted():
    from gridtrade.backtest.backtest_run import resolve_bt_universe
    arch = ['AAA/USDT:USDT', 'TRAD/USDT:USDT', 'LOW/USDT:USDT',
            'DEAD/USDT:USDT',            # 退市:不在 markets/档位表 → 双过滤都保留
            'BL/USDT:USDT']              # 黑名单
    uni, stats = resolve_bt_universe(_uni_adapter(), ['BL/USDT:USDT'],
                                     archive_symbols=arch, min_lev=10.0,
                                     log=lambda *a: None)
    assert uni == ['AAA/USDT:USDT', 'DEAD/USDT:USDT']   # 非COIN剔/低杠杆剔/退市留/黑名单剔
    assert stats == {'n_blacklist': 1, 'n_tradfi': 1, 'n_lowlev': 1, 'min_lev': 10.0}


def test_resolve_bt_universe_minlev_zero_bypasses_leverage_filter():
    from gridtrade.backtest.backtest_run import resolve_bt_universe
    arch = ['AAA/USDT:USDT', 'LOW/USDT:USDT']
    uni, stats = resolve_bt_universe(_uni_adapter(), (), archive_symbols=arch,
                                     min_lev=0.0, log=lambda *a: None)
    assert uni == ['AAA/USDT:USDT', 'LOW/USDT:USDT']    # =0 显式停用回旧口径
    assert stats['n_lowlev'] == 0


def test_resolve_bt_universe_env_default_minlev(monkeypatch):
    from gridtrade.backtest.backtest_run import resolve_bt_universe
    monkeypatch.delenv('BT_MIN_LEVERAGE', raising=False)
    _, stats = resolve_bt_universe(_uni_adapter(), (), archive_symbols=['AAA/USDT:USDT'],
                                   log=lambda *a: None)
    assert stats['min_lev'] == 10.0                     # min_lev=None → env 默认 10.0


def test_resolve_bt_universe_n_blacklist_is_raw_length_not_intersection():
    # 锁语义:n_blacklist=黑名单原始长度(原 main() 打印 len(bt_blacklist) 的口径)——
    # 黑名单含不在归档的符号(GHOST)时,交集口径会少报,破坏统计 print 行逐字节等价。
    from gridtrade.backtest.backtest_run import resolve_bt_universe
    arch = ['AAA/USDT:USDT', 'BL/USDT:USDT']
    bl = ['BL/USDT:USDT', 'GHOST/USDT:USDT']            # GHOST 不在归档
    uni, stats = resolve_bt_universe(_uni_adapter(), bl, archive_symbols=arch,
                                     min_lev=10.0, log=lambda *a: None)
    assert uni == ['AAA/USDT:USDT']
    assert stats['n_blacklist'] == len(bl)              # 原始长度=2,而非交集=1


def test_sweep_run_uses_shared_universe_builder(monkeypatch):
    """sweep_run 票池必须走 resolve_bt_universe(口径分叉回归锁,spec 2026-07-24)。"""
    import scripts.sweep_run as SRU
    calls = {}

    def fake_ds(cache):
        return _uni_adapter(), None

    def fake_resolve(adapter, blacklist, **kw):
        calls['blacklist'] = tuple(blacklist)
        return ['AAA/USDT:USDT'], {'n_tradfi': 0, 'n_lowlev': 0,
                                   'n_blacklist': 0, 'min_lev': 10.0}

    monkeypatch.setattr(SRU, '_binance_datasource_1h', fake_ds)
    monkeypatch.setattr(SRU, 'resolve_bt_universe', fake_resolve)
    uni = SRU.resolve_sweep_universe(cache=None)
    assert uni == ['AAA/USDT:USDT']
    assert len(calls['blacklist']) > 0          # tier0 黑名单已传入
