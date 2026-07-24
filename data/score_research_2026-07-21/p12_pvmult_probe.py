"""p12 pv尖峰敏感度(pv_mult)探针(2026-07-25,基座=f1%×k0.15/固3%/pv_thr-0.01):
mult=量能超rolling基线几倍判尖峰(现值3,n=100根15min)。前科:SWEEP3在现役人群
mult{5,8}=W1惨案(冲洗日全币同飙→V底集体割肉)。p12人群假设反转:燃料币量能常态
尖峰,mult3或天天误触发→调高=只拦真异常。臂: M2x2 / M5x5 / M8x8(各自重算pv_df)。
复用 p12k_*.parquet 格;K1f1k.15列=mult3基线。
用法: p12_pvmult_probe.py
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

SCFG = dict(stop_loss=0.03, trailing_k=0.15, trailing_floor=0.01,
            fundingRate_stop_loss=_STOP['fundingRate_stop_loss'])
PVT = _STOP['pv_pnl_thr']
MULTS = {'M2x2': 2, 'M5x5': 5, 'M8x8': 8}


def main():
    cache = ParquetCache(V.default_cache_root())
    print('窗       变体        alpha(bp) mean/med/t/胜率   绝对均值bp')
    for wn in ('W1', 'W2', 'OOS', 'HOLD-A', 'HOLD-B'):
        P = pd.read_parquet(f'{RD}/ablation/p12k_{wn}.parquet')
        m1m, fdm = {}, {}
        out = {k: [] for k in MULTS}
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
                for k in MULTS:
                    out[k].append(np.nan)
                continue
            fds = cf_eval.slice_funding(fd, bars)
            gp = cf_eval.gp_v2(r['Atr_5'], float(bars['open'].iloc[0]))
            for k, mult in MULTS.items():
                try:
                    pv_cfg = dict(cf_eval.PV_CFG)
                    pv_cfg['mult'] = mult
                    pv_df = pv_spike_for_window(m1, bars, pv_cfg)
                    res = simulate_grid_engine(
                        bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
                        fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                        stop_cfg=dict(SCFG), funding_df=fds, pv_spike_df=pv_df,
                        neutral_init=False, active_stop_mode='pv', pv_pnl_thr=PVT)
                    out[k].append(float(res['pnl_ratio']))
                except Exception:
                    out[k].append(np.nan)
            if len(m1m) > 150:
                m1m.clear()
                fdm.clear()
        for k in MULTS:
            P[k] = out[k]
        cols = ['s030', 'K1f1k.15'] + list(MULTS)
        for k in cols:
            a = (P[k] - P['pool_s']).dropna() * 1e4
            print('%-8s %-9s %+7.1f/%+6.1f/t%+5.2f/%.2f   %+7.1f  n=%d'
                  % (wn, k if k != 's030' else 's030基线', a.mean(), a.median(),
                     a.mean() / (a.std() / np.sqrt(len(a))), (a > 0).mean(),
                     P[k].dropna().mean() * 1e4, len(P)), flush=True)
        P.to_parquet(f'{RD}/ablation/p12pvmult_{wn}.parquet')


if __name__ == '__main__':
    main()
