"""p12 pv/固损重扫探针(2026-07-25,基座=f1%×k0.15 新trailing):
坐标下降式两轴(避免全格点臂数爆炸),基座 stop_cfg={stop3%,k0.15,floor1%,funding}:
  P1 pv宽-0.02   P2 pv全关(mode none)
  S1 固损2%      S2 固损5%      S3 固损关(9.0中和,消融同款)
复用 p12k_*.parquet 的格与 K1f1k.15(=基座,pv-0.01/固3%)列。
用法: p12_pvstop_probe.py
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
TR = {'trailing_k': 0.15, 'trailing_floor': 0.01}
NOFIX = 9.0
# (stop_cfg, mode, pv_thr)
VARIANTS = {
    'P1pv-2%':  (dict(stop_loss=0.03, **TR, **FR), 'pv', -0.02),
    'P2pv关':   (dict(stop_loss=0.03, **TR, **FR), 'none', None),
    'S1固2%':   (dict(stop_loss=0.02, **TR, **FR), 'pv', _STOP['pv_pnl_thr']),
    'S2固5%':   (dict(stop_loss=0.05, **TR, **FR), 'pv', _STOP['pv_pnl_thr']),
    'S3固关':   (dict(stop_loss=NOFIX, **TR, **FR), 'pv', _STOP['pv_pnl_thr']),
}


def main():
    cache = ParquetCache(V.default_cache_root())
    print('窗       变体        alpha(bp) mean/med/t/胜率   绝对均值bp')
    for wn in ('W1', 'W2', 'OOS', 'HOLD-A', 'HOLD-B'):
        P = pd.read_parquet(f'{RD}/ablation/p12k_{wn}.parquet')
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
            pv_df = None
            for k, (scfg, mode, thr) in VARIANTS.items():
                try:
                    kw = dict(cap=1000.0, leverage=GEARING / MAX_RATE, fee=FEE_MAKER,
                              c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                              stop_cfg=dict(scfg), funding_df=fds,
                              neutral_init=False, active_stop_mode=mode)
                    if mode == 'pv':
                        if pv_df is None:
                            pv_df = pv_spike_for_window(m1, bars, cf_eval.PV_CFG)
                        kw['pv_spike_df'] = pv_df
                        kw['pv_pnl_thr'] = thr
                    else:
                        kw['pv_spike_df'] = None
                    res = simulate_grid_engine(bars, gp, **kw)
                    out[k].append(float(res['pnl_ratio']))
                except Exception:
                    out[k].append(np.nan)
            if len(m1m) > 150:
                m1m.clear()
                fdm.clear()
        for k in VARIANTS:
            P[k] = out[k]
        cols = ['s030', 'K1f1k.15'] + list(VARIANTS)
        for k in cols:
            a = (P[k] - P['pool_s']).dropna() * 1e4
            print('%-8s %-9s %+7.1f/%+6.1f/t%+5.2f/%.2f   %+7.1f  n=%d'
                  % (wn, k if k != 's030' else 's030基线', a.mean(), a.median(),
                     a.mean() / (a.std() / np.sqrt(len(a))), (a > 0).mean(),
                     P[k].dropna().mean() * 1e4, len(P)), flush=True)
        P.to_parquet(f'{RD}/ablation/p12pvstop_{wn}.parquet')


if __name__ == '__main__':
    main()
