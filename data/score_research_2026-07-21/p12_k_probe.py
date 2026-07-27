"""p12 trailing_k 变体探针(2026-07-25,续 p12_trail_probe):
k=回吐闸的峰值比例(max(floor,k×峰值));现役人群死轴(峰值小,floor主导),p12 冲高
5~20% 使 k 绑定复活。臂位(复用 p12trail_*.parquet 的格与 T2/floor1%k0.3 列):
  K1 floor1% k0.15  大冲高只许回吐15%——激进落袋
  K2 floor1% k0.6   宽容回吐60%——给弹跳留空间
  K3 floor2% k0.15  现floor×激进k 对照
用法: p12_k_probe.py
"""
import importlib.util
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import pv_spike_for_window
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.sweep import FEE_MAKER, FEE_TAKER, GEARING, MAX_RATE, _STOP

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_s = importlib.util.spec_from_file_location('cf_eval', RD + '/cf_eval.py')
cf_eval = importlib.util.module_from_spec(_s)
_s.loader.exec_module(cf_eval)
from gridtrade.core.grid_engine import simulate_grid_engine  # noqa: E402

FR = {'fundingRate_stop_loss': _STOP['fundingRate_stop_loss']}
PVT = _STOP['pv_pnl_thr']
VARIANTS = {
    'K1f1k.15': dict(stop_loss=0.03, trailing_k=0.15, trailing_floor=0.01, **FR),
    'K2f1k.6':  dict(stop_loss=0.03, trailing_k=0.60, trailing_floor=0.01, **FR),
    'K3f2k.15': dict(stop_loss=0.03, trailing_k=0.15, trailing_floor=0.02, **FR),
}


def main():
    cache = ParquetCache(V.default_cache_root())
    print('窗       变体        alpha(bp) mean/med/t/胜率   绝对均值bp')
    for wn in ('W1', 'W2', 'OOS', 'HOLD-A', 'HOLD-B'):
        P = pd.read_parquet(f'{RD}/ablation/p12trail_{wn}.parquet')
        m1m, fdm = {}, {}
        out = {k: [] for k in VARIANTS}
        for _, r in P.iterrows():
            sym, rt = r['symbol'], pd.Timestamp(r['run_time'])
            m1 = m1m.get(sym)
            if m1 is None:
                m1 = cache.read_all_days('1m', sym)
                m1m[sym] = m1
            fd = fdm.get(sym)
            if fd is None:
                fd = cache.read_all_days('funding', sym)
                fdm[sym] = fd
            bars = cf_eval.prep_window(m1, rt)
            if bars is None:
                for k in VARIANTS:
                    out[k].append(np.nan)
                continue
            fds = cf_eval.slice_funding(fd, bars)
            gp = cf_eval.gp_v2(r['Atr_5'], float(bars['open'].iloc[0]))
            pv_df = pv_spike_for_window(m1, bars, cf_eval.PV_CFG)
            for k, scfg in VARIANTS.items():
                try:
                    res = simulate_grid_engine(
                        bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
                        fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                        stop_cfg=dict(scfg), funding_df=fds, pv_spike_df=pv_df,
                        neutral_init=False, active_stop_mode='pv', pv_pnl_thr=PVT)
                    out[k].append(float(res['pnl_ratio']))
                except Exception:
                    out[k].append(np.nan)
            if len(m1m) > 150:
                m1m.clear()
                fdm.clear()
        for k in VARIANTS:
            P[k] = out[k]
        n = len(P)
        cols = ['s030', 'T2地板1%'] + list(VARIANTS)
        for k in cols:
            a = (P[k] - P['pool_s']).dropna() * 1e4
            print('%-8s %-9s %+7.1f/%+6.1f/t%+5.2f/%.2f   %+7.1f  n=%d'
                  % (wn, k if k != 's030' else 's030基线', a.mean(), a.median(),
                     a.mean() / (a.std() / np.sqrt(len(a))), (a > 0).mean(),
                     P[k].dropna().mean() * 1e4, n), flush=True)
        P.to_parquet(f'{RD}/ablation/p12k_{wn}.parquet')


if __name__ == '__main__':
    main()
