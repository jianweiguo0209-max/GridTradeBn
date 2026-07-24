"""p12专属止损链变体探针(2026-07-25,用户假设"高燃料人群该配另一套止损"):
只对 p12top 选中格(≈245格/窗×5已完成窗)重放三套链变体,与既有 E0/s030 并陈:
  V1 无pv     固损3%+trailing2%+funding(mode='none')——验"pv对高燃料币退化成裸-1%止损"
  V2 pv宽     全链但 pv_pnl_thr=-0.02
  V3 固5无pv  固损5%+trailing2%+funding(mode='none')
alpha 口径=变体均值−池s030均值(与部署对照同构)。探索读数,不触 IS,不改预注册判据。
用法: p12_chain_probe.py
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

LAB = {'W1': f'{RD}/sc_labels_W1.parquet', 'W2': f'{RD}/sc_labels_W2.parquet',
       'OOS': f'{RD}/sc_labels_OOS.parquet',
       'HOLD-A': f'{RD}/ablation/hold_labels_HOLD-A.parquet',
       'HOLD-B': f'{RD}/ablation/hold_labels_HOLD-B.parquet'}
TRAIL = {'trailing_k': _STOP['trailing_k'], 'trailing_floor': _STOP['trailing_floor']}
FR = {'fundingRate_stop_loss': _STOP['fundingRate_stop_loss']}
VARIANTS = {
    'V1无pv':   (dict(stop_loss=0.03, **TRAIL, **FR), 'none', None),
    'V2pv宽':   (dict(stop_loss=0.03, **TRAIL, **FR), 'pv', -0.02),
    'V3固5无pv': (dict(stop_loss=0.05, **TRAIL, **FR), 'none', None),
}


def run_variant(m1, bars, gp, fd, scfg, mode, pv_thr):
    kw = dict(cap=1000.0, leverage=GEARING / MAX_RATE, fee=FEE_MAKER,
              c_rate_taker=FEE_TAKER, max_rate=MAX_RATE, stop_cfg=scfg,
              funding_df=fd, neutral_init=False, active_stop_mode=mode)
    if mode == 'pv':
        kw['pv_spike_df'] = pv_spike_for_window(m1, bars, cf_eval.PV_CFG)
        kw['pv_pnl_thr'] = pv_thr
    else:
        kw['pv_spike_df'] = None
    res = simulate_grid_engine(bars, gp, **kw)
    return float(res['pnl_ratio'])


def main():
    cache = ParquetCache(V.default_cache_root())
    print('窗       变体        alpha(bp) mean/med/t/胜率   绝对均值bp')
    for wn in ('W1', 'W2', 'OOS', 'HOLD-A', 'HOLD-B'):
        cf = pd.read_parquet(f'{RD}/ablation/cf_{wn}.parquet')
        lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1']].rename(
            columns={'cross1': 'p12'})
        lab['run_time'] = lab['rt'] + pd.Timedelta(hours=12)
        cf = cf.merge(lab[['run_time', 'symbol', 'p12']],
                      on=['run_time', 'symbol'], how='left')
        picks = []
        for rt, g in cf.groupby('run_time'):
            gp_ = g[g['in_pool']]
            pk = g[g['picked']]
            if gp_.empty or pk.empty:
                continue
            av = gp_[np.isfinite(gp_['p12'])]
            if len(av) < 30:
                continue
            t = av.nlargest(len(pk), 'p12')
            for _, r in t.iterrows():
                picks.append({'run_time': rt, 'symbol': r['symbol'],
                              'Atr_5': r['Atr_5'], 'pool_s': gp_['pnl_s030'].mean(),
                              'e0': r['pnl_e0'], 's030': r['pnl_s030']})
        P = pd.DataFrame(picks)
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
            for k, (scfg, mode, thr) in VARIANTS.items():
                try:
                    out[k].append(run_variant(m1, bars, gp, fds, dict(scfg), mode, thr))
                except Exception:
                    out[k].append(np.nan)
            if len(m1m) > 150:
                m1m.clear()
                fdm.clear()
        for k in VARIANTS:
            P[k] = out[k]
        n = len(P)
        for k in ['s030'] + list(VARIANTS):
            a = (P[k] - P['pool_s']).dropna() * 1e4
            print('%-8s %-9s %+7.1f/%+6.1f/t%+5.2f/%.2f   %+7.1f  n=%d'
                  % (wn, k if k != 's030' else 's030基线', a.mean(), a.median(),
                     a.mean() / (a.std() / np.sqrt(len(a))), (a > 0).mean(),
                     P[k].dropna().mean() * 1e4, n), flush=True)
        P.to_parquet(f'{RD}/ablation/p12chain_{wn}.parquet')


if __name__ == '__main__':
    main()
