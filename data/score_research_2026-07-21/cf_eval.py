# data/score_research_2026-07-21/cf_eval.py
"""反事实选币评价器核心(spec docs/superpowers/specs/2026-07-23-counterfactual-selection-evaluator-design.md):
对 (rt, symbol) 在已实现 [rt, rt+12h) 上跑双口径标定引擎——
  pnl_e0   纯网格无退出链(主分数=走势固有网格适配度)
  pnl_s030 全止损链(副刻度=系统实收)
geometry='v2' 生产几何(calc_grid_params_v2, config 现值);'geo' 锚模式(geo_sweep_run
的 m30_c16 公式,只用于对 geo_* 复现)。协议与 s030_calib/geo_sweep_run 逐位同构。
"""
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest.backtest_run import (_FUNDING_BACK_MS, holding_bars,
                                             pv_spike_for_window)
from gridtrade.backtest.sweep import (FEE_MAKER, FEE_TAKER, GEARING, MAX_RATE,
                                      _S, _STOP, _V2)
from gridtrade.core.grid_engine import simulate_grid_engine
from gridtrade.core.grid_params import calc_grid_params_v2

STOP_CFG = {'stop_loss': _STOP['stop_loss'], 'trailing_k': _STOP['trailing_k'],
            'trailing_floor': _STOP['trailing_floor'],
            'fundingRate_stop_loss': _STOP['fundingRate_stop_loss']}
PV_CFG = {'mult': _STOP['pv_mult'], 'n': _STOP['pv_n'], 'period': _STOP['pv_period']}


def prep_window(m1, rt):
    """12h 持仓窗 bars;数据不足(<600 根)返回 None——与 s030_calib/geo 同判。"""
    if m1 is None or m1.empty:
        return None
    bars = holding_bars(m1, pd.Timestamp(rt), _S['period'])
    if len(bars) < 600:
        return None
    return bars


def slice_funding(fd, bars):
    if fd is None or fd.empty:
        return fd
    lo = int(bars['candle_begin_time'].min().value // 1_000_000)
    hi = int(bars['candle_begin_time'].max().value // 1_000_000)
    return fd[(fd['ts'] >= lo - _FUNDING_BACK_MS) & (fd['ts'] <= hi)]


def gp_v2(atr5, close):
    """生产几何:V2 + config 现值,middle_5≈close(Stage E 备案)。"""
    gr = {'Atr_5': float(atr5), 'close': close, 'middle_5': close}
    return calc_grid_params_v2(gr, _S['price_limit'], _S['stop_limit'], _V2)


def gp_geo(atr5, close):
    """锚模式:geo_sweep_run.make_gp 的 m30_c16(clip 0.02~0.5,±1% buffer,固定16格)。
    只用于对 geo_* 复现;生产口径一律 gp_v2。"""
    r = min(max(3.0 * float(atr5), 0.02), 0.5)
    return {'high_price': close * (1 + r), 'low_price': close * (1 - r),
            'stop_high_price': close * (1 + r) * 1.01,
            'stop_low_price': close * (1 - r) * 0.99, 'grid_count': 16}


def run_engine(m1, bars, gp, fd, full_chain):
    """单口径引擎调用 → (pnl, reason)。True=s030 全链(逐参同 s030_calib);
    False=E0(逐参同 geo_sweep_run)。"""
    if full_chain:
        pv_df = pv_spike_for_window(m1, bars, PV_CFG)
        res = simulate_grid_engine(
            bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
            fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
            stop_cfg=STOP_CFG, funding_df=fd, pv_spike_df=pv_df,
            neutral_init=False, active_stop_mode='pv',
            pv_pnl_thr=_STOP['pv_pnl_thr'])
    else:
        res = simulate_grid_engine(
            bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
            fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
            stop_cfg=None, funding_df=fd, pv_spike_df=None,
            neutral_init=False, active_stop_mode='none')
    return float(res['pnl_ratio']), res.get('exit_reason', '?')


def eval_grid(m1, fd_all, rt, atr5, geometry='v2'):
    """一格双口径。返回 {'pnl_e0','reason_e0','pnl_s030','reason_s030'} 或 None。"""
    if atr5 is None or not np.isfinite(atr5):
        return None
    bars = prep_window(m1, rt)
    if bars is None:
        return None
    fd = slice_funding(fd_all, bars)
    close = float(bars['open'].iloc[0])
    gp = gp_v2(atr5, close) if geometry == 'v2' else gp_geo(atr5, close)
    p0, r0 = run_engine(m1, bars, gp, fd, full_chain=False)
    p1, r1 = run_engine(m1, bars, gp, fd, full_chain=True)
    return {'pnl_e0': p0, 'reason_e0': r0, 'pnl_s030': p1, 'reason_s030': r1}
