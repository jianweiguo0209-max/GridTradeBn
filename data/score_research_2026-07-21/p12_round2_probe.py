"""p12 坐标下降第二轮探针(2026-07-25,基座=f1k.15/固3%/pv thr−1%/mult5):
  thr轴: Th05(−0.5%) Th20(−2%)          [共享 mult5/n100 尖峰序列]
  n轴:  N50(基线50根) N200(200根)       [各自重算尖峰序列]
  固损轴: St4(4%) St5(5%) St7(7%)
复用 p12pvmult_*.parquet 格;M5x5 列=基座参照。
用法: p12_round2_probe.py
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


def scfg(stop):
    return dict(stop_loss=stop, **TR, **FR)


# (stop, pv_key, thr)
VARIANTS = {
    'Th05': (0.03, 'n100', -0.005),
    'Th20': (0.03, 'n100', -0.02),
    'N50':  (0.03, 'n50', -0.01),
    'N200': (0.03, 'n200', -0.01),
    'St4':  (0.04, 'n100', -0.01),
    'St5':  (0.05, 'n100', -0.01),
    'St7':  (0.07, 'n100', -0.01),
}
PV_CFGS = {'n100': {'mult': 5, 'n': 100, 'period': '15min'},
           'n50':  {'mult': 5, 'n': 50, 'period': '15min'},
           'n200': {'mult': 5, 'n': 200, 'period': '15min'}}


def main():
    cache = ParquetCache(V.default_cache_root())
    print('窗       变体        alpha(bp) mean/med/t/胜率   绝对均值bp')
    for wn in ('W1', 'W2', 'OOS', 'HOLD-A', 'HOLD-B'):
        P = pd.read_parquet(f'{RD}/ablation/p12pvmult_{wn}.parquet')
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
            pv_dfs = {}
            for k, (stop, pvk, thr) in VARIANTS.items():
                try:
                    if pvk not in pv_dfs:
                        pv_dfs[pvk] = pv_spike_for_window(m1, bars, PV_CFGS[pvk])
                    res = simulate_grid_engine(
                        bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
                        fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                        stop_cfg=scfg(stop), funding_df=fds, pv_spike_df=pv_dfs[pvk],
                        neutral_init=False, active_stop_mode='pv', pv_pnl_thr=thr)
                    out[k].append(float(res['pnl_ratio']))
                except Exception:
                    out[k].append(np.nan)
            if len(m1m) > 150:
                m1m.clear()
                fdm.clear()
        for k in VARIANTS:
            P[k] = out[k]
        cols = ['s030', 'M5x5'] + list(VARIANTS)
        for k in cols:
            a = (P[k] - P['pool_s']).dropna() * 1e4
            print('%-8s %-9s %+7.1f/%+6.1f/t%+5.2f/%.2f   %+7.1f  n=%d'
                  % (wn, k if k != 's030' else 's030基线', a.mean(), a.median(),
                     a.mean() / (a.std() / np.sqrt(len(a))), (a > 0).mean(),
                     P[k].dropna().mean() * 1e4, len(P)), flush=True)
        P.to_parquet(f'{RD}/ablation/p12r2_{wn}.parquet')


if __name__ == '__main__':
    main()
