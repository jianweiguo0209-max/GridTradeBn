"""p12 资金费率止损阈值探针(2026-07-25,基座=v2/St5: f1k.15/mult5/thr-1%/固5%):
现值 |funding|>0.0015 退出。前情:现役人群A族战役15臂全灭raw站住;W2法医显示
此层在meme雷上活跃(19/47雷格由它退出)。臂:
  F05=0.05%(紧2倍)  F10=0.10%  F30=0.30%(松2倍)  F99=关(1.0不可达)
复用 p12r2_*.parquet 格;St5列=基座参照(funding 0.15%)。
用法: p12_funding_probe.py
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

TR = {'trailing_k': 0.15, 'trailing_floor': 0.01}
PVC5 = {'mult': 5, 'n': 100, 'period': '15min'}
PVT = _STOP['pv_pnl_thr']
VARIANTS = {
    'F05紧': 0.0005,
    'F10':   0.0010,
    'F30松': 0.0030,
    'F99关': 1.0,
}


def main():
    cache = ParquetCache(V.default_cache_root())
    print('窗       变体        alpha(bp) mean/med/t/胜率   绝对均值bp')
    for wn in ('W1', 'W2', 'OOS', 'HOLD-A', 'HOLD-B'):
        P = pd.read_parquet(f'{RD}/ablation/p12r2_{wn}.parquet')
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
            pv_df = pv_spike_for_window(m1, bars, PVC5)
            for k, fr in VARIANTS.items():
                try:
                    scfg = dict(stop_loss=0.05, **TR, fundingRate_stop_loss=fr)
                    res = simulate_grid_engine(
                        bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
                        fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                        stop_cfg=scfg, funding_df=fds, pv_spike_df=pv_df,
                        neutral_init=False, active_stop_mode='pv', pv_pnl_thr=PVT)
                    out[k].append(float(res['pnl_ratio']))
                except Exception:
                    out[k].append(np.nan)
            if len(m1m) > 150:
                m1m.clear()
                fdm.clear()
        for k in VARIANTS:
            P[k] = out[k]
        cols = ['s030', 'St5'] + list(VARIANTS)
        for k in cols:
            a = (P[k] - P['pool_s']).dropna() * 1e4
            print('%-8s %-9s %+7.1f/%+6.1f/t%+5.2f/%.2f   %+7.1f  n=%d'
                  % (wn, k if k != 's030' else 's030基线', a.mean(), a.median(),
                     a.mean() / (a.std() / np.sqrt(len(a))), (a > 0).mean(),
                     P[k].dropna().mean() * 1e4, len(P)), flush=True)
        P.to_parquet(f'{RD}/ablation/p12fr_{wn}.parquet')


if __name__ == '__main__':
    main()
