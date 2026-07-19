"""回测 pv 语义须等于实盘（spec 2026-07-15-binance-param-sweep §0）。

实盘 LiveSignalProvider 取 n+8 根原生 15m（≈27h 前置历史）算 rolling(n) 量能基线；
回测若只用 12h 持仓窗内的 48 根 15m + min_periods=1，基线退化成「窗口内扩张均值」——
开窗头几根样本数仅 1-2、且看不到开仓前的量能水位，会把「相对窗内前几根偏大、但相对
真实 25h 基线其实正常」的量误判为尖峰（pv 主动止损=最大单项退出驱动，实测砍 ~49% 的格）。

守卫：assemble_grid_tasks 须逐格预算 pv_spike_df（含 27h 前置历史）并下传引擎。
"""
import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import calc_pv_spike

_PV_N = 100
_PV_MULT = 3
_PV_PERIOD = '15min'


def _series_with_high_prehistory(sym='AAA/USDT:USDT'):
    """构造 1m 序列：窗口前 30h 量能高位(baseline≈100)，持仓窗内量能中位(≈40)。
    真实 25h 基线下窗内没有任何尖峰（40 < 3×100）；但若只看窗内、用扩张均值，
    开窗首根 15m 的基线=它自己 → 之后任何 >3× 窗内早期均值的量都会被误判为尖峰。
    故窗内前段刻意压低量能(≈5)、后段回到 40 → 窗内近似必然误报，实盘口径必然不报。"""
    t0 = pd.Timestamp('2026-03-01 00:00:00')            # 持仓窗起点
    pre_start = t0 - pd.Timedelta(hours=30)
    idx = pd.date_range(pre_start, t0 + pd.Timedelta(hours=12), freq='1min')[:-1]
    n = len(idx)
    qv = np.full(n, 100.0)                              # 前置历史：高位量能
    in_win = idx >= t0
    qv[in_win] = 5.0                                    # 窗内前段：极低量能（做低窗内基线）
    late = idx >= t0 + pd.Timedelta(hours=6)
    qv[late] = 40.0                                     # 窗内后段：中位量能（<3×100，实盘不算尖峰）
    px = np.full(n, 10.0)
    return pd.DataFrame({
        'symbol': sym, 'candle_begin_time': idx,
        'open': px, 'high': px * 1.001, 'low': px * 0.999, 'close': px,
        'vol': qv / px, 'volCcy': qv, 'quote_volume': qv,
    })


def test_window_only_baseline_misfires_but_prehistory_does_not():
    """钉住两种口径的实质差异（此测试证明前置历史不是无关紧要的细节）。"""
    full = _series_with_high_prehistory()
    t0 = pd.Timestamp('2026-03-01 00:00:00')
    bars = full[full['candle_begin_time'] >= t0].reset_index(drop=True)

    only_win = calc_pv_spike(bars, active_period=_PV_PERIOD, mult=_PV_MULT, n=_PV_N)
    assert only_win['pv_spike'].sum() > 0, '窗口内近似应误报尖峰（基线被窗内低量压低）'

    with_pre = calc_pv_spike(full, active_period=_PV_PERIOD, mult=_PV_MULT, n=_PV_N)
    in_win = with_pre[with_pre['candle_begin_time'] >= t0]
    assert in_win['pv_spike'].sum() == 0, '含 27h 前置历史（实盘口径）不应报尖峰'


def test_assemble_grid_tasks_carries_prehistory_pv(monkeypatch, tmp_path):
    """assemble_grid_tasks 必须给每格带上按前置历史算的 pv_spike_df（下传引擎）。"""
    from gridtrade.backtest import backtest_run as BR
    from gridtrade.backtest import selection_replay as SR

    sym = 'AAA/USDT:USDT'
    full = _series_with_high_prehistory(sym)
    monkeypatch.setattr(SR, 'load_full_series', lambda cache, syms, tf: {sym: full})

    class _Cache:
        def read_all_days(self, ns, s):
            return pd.DataFrame(columns=['ts', 'symbol', 'fundingRate', 'realizedRate'])

    rt = pd.Timestamp('2026-03-01 00:00:00')
    row = pd.Series({'symbol': sym, 'close': 10.0, 'Atr_5': 0.02, 'middle_5': 10.0})
    strategy = {'period': '12H', 'price_limit': [0.25, 0.25], 'stop_limit': 0.01,
                'grid_version': 2,
                'grid_v2_config': {'atr_range_multiplier': 2, 'range_pct_min': 0.05,
                                   'range_pct_max': 0.50, 'grid_spacing_atr_ratio': 0.5,
                                   'grid_spacing_min': 0.003, 'grid_spacing_max': 0.04,
                                   'grid_count_min': 10, 'grid_count_max': 149,
                                   'stop_buffer_ratio': 0.01},
                'pv_config': {'mult': _PV_MULT, 'period': _PV_PERIOD, 'n': _PV_N}}

    tasks = BR.assemble_grid_tasks(_Cache(), [(rt, 0, row)], strategy,
                                   sim_timeframe='1m', timeframe='1h', log=lambda *a: None)
    assert len(tasks) == 1
    pv_df = tasks[0][BR.TASK_PV_IDX]
    assert pv_df is not None, 'data_task 必须携带预算的 pv_spike_df'
    assert list(pv_df.columns) == ['candle_begin_time', 'pv_spike', 'pv_dir']  # +方向列(spec pv-directional)
    # 关键：用了前置历史 → 窗内零尖峰（若退化成窗口内近似，这里会 >0）
    assert int(pv_df['pv_spike'].sum()) == 0, 'pv 尖峰须按 27h 前置历史算（实盘同源）'
    bars = tasks[0][5]
    assert len(pv_df) == len(bars), 'pv 序列须与持仓 bars 逐根对齐'
