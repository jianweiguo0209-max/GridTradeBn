"""p12 trailing 锁盈层变体探针(2026-07-25,续 p12_chain_probe):
其余层保持 s030 现值(pv−0.01/固损3%/funding),只动 trailing:
  T1地板4%  峰值>4%才武装、容忍4%回吐——让高燃料冲高多跑
  T2地板1%  更早武装更紧锁
  T3无锁盈  trailing 键整体缺省(引擎 get→None 跳过)
对 p12top 选中格重放,alpha=变体均值−池s030均值。探索读数,不触 IS。
用法: p12_trail_probe.py
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
K = _STOP['trailing_k']
FR = {'fundingRate_stop_loss': _STOP['fundingRate_stop_loss']}
PVT = _STOP['pv_pnl_thr']
VARIANTS = {
    'T1地板4%': dict(stop_loss=0.03, trailing_k=K, trailing_floor=0.04, **FR),
    'T2地板1%': dict(stop_loss=0.03, trailing_k=K, trailing_floor=0.01, **FR),
    'T3无锁盈':  dict(stop_loss=0.03, **FR),
}


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
                              's030': r['pnl_s030']})
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
        for k in ['s030'] + list(VARIANTS):
            a = (P[k] - P['pool_s']).dropna() * 1e4
            print('%-8s %-9s %+7.1f/%+6.1f/t%+5.2f/%.2f   %+7.1f  n=%d'
                  % (wn, k if k != 's030' else 's030基线', a.mean(), a.median(),
                     a.mean() / (a.std() / np.sqrt(len(a))), (a > 0).mean(),
                     P[k].dropna().mean() * 1e4, n), flush=True)
        P.to_parquet(f'{RD}/ablation/p12trail_{wn}.parquet')


if __name__ == '__main__':
    main()
