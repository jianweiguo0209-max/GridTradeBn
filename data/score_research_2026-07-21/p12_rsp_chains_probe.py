"""RSP111(Reg↑+Sgcz↑+p12↓等权) × {St4,F99} 链探针(2026-07-25,补即时探针的链对比列):
RSP 选中格重放两链;alpha 基线=池 s030 均值(与此前链探针同口径,部署对照语义)。
用法: p12_rsp_chains_probe.py
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

FAC = {w: f'{RD}/sc_factors_{w}.parquet' for w in ('W1', 'W2', 'OOS', 'IS')}
FAC.update({w: f'{RD}/ablation/hold_factors_{w}.parquet' for w in ('HOLD-A', 'HOLD-B')})
LAB = {w: f'{RD}/sc_labels_{w}.parquet' for w in ('W1', 'W2', 'OOS', 'IS')}
LAB.update({w: f'{RD}/ablation/hold_labels_{w}.parquet' for w in ('HOLD-A', 'HOLD-B')})
WD = {'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
      'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
      'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30')}
TR = {'trailing_k': 0.15, 'trailing_floor': 0.01}
CHAINS = {
    'v2固3': dict(stop_loss=0.03, **TR, fundingRate_stop_loss=_STOP['fundingRate_stop_loss']),
    'St4':  dict(stop_loss=0.04, **TR, fundingRate_stop_loss=_STOP['fundingRate_stop_loss']),
    'St5':  dict(stop_loss=0.05, **TR, fundingRate_stop_loss=_STOP['fundingRate_stop_loss']),
    'F30':  dict(stop_loss=0.05, **TR, fundingRate_stop_loss=0.003),
    'F99':  dict(stop_loss=0.05, **TR, fundingRate_stop_loss=1.0),
}
PVC5 = {'mult': 5, 'n': 100, 'period': '15min'}


def main():
    cache = ParquetCache(V.default_cache_root())
    pooled = {cn: [] for cn in CHAINS}
    print('窗      链   alpha(bp)/t (基线=池s030均值)  n')
    for wn in ('W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B'):
        cf = pd.read_parquet(f'{RD}/ablation/cf_{wn}.parquet')
        fac = pd.read_parquet(FAC[wn])[['rt', 'symbol', 'Reg_v2_5', 'Sgcz_5']].rename(
            columns={'rt': 'run_time'})
        lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1']].rename(
            columns={'cross1': 'p12'})
        lab['run_time'] = lab['rt'] + pd.Timedelta(hours=12)
        cf = cf.merge(fac, on=['run_time', 'symbol'], how='left').merge(
            lab[['run_time', 'symbol', 'p12']], on=['run_time', 'symbol'], how='left')
        picks = []
        for rt, g in cf.groupby('run_time'):
            gp = g[g['in_pool']]
            av = gp.dropna(subset=['Reg_v2_5', 'Sgcz_5', 'p12'])
            if len(av) < 30:
                continue
            av = av.copy()
            av['rs'] = (av['Reg_v2_5'].rank(ascending=True, method='first')
                        + av['Sgcz_5'].rank(ascending=True, method='first')
                        + av['p12'].rank(ascending=False, method='first'))
            pick = av.nsmallest(1, 'rs').iloc[0]
            picks.append({'run_time': rt, 'symbol': pick['symbol'],
                          'Atr_5': float(pick['Atr_5']),
                          'pool_s': gp['pnl_s030'].mean()})
        m1lo = pd.Timestamp(WD[wn][0]) - pd.Timedelta(days=2)
        m1hi = pd.Timestamp(WD[wn][1]) + pd.Timedelta(days=2)
        m1m, fdm = {}, {}
        res = {cn: [] for cn in CHAINS}
        for r in picks:
            sym, rt = r['symbol'], pd.Timestamp(r['run_time'])
            m1 = m1m.get(sym)
            if m1 is None:
                m1 = cache.read_days_range('1m', sym, m1lo.strftime('%Y-%m-%d'),
                                           m1hi.strftime('%Y-%m-%d'))
                m1m[sym] = m1
            fd = fdm.get(sym)
            if fd is None:
                fd = cache.read_all_days('funding', sym)
                fdm[sym] = fd
            bars = cf_eval.prep_window(m1, rt)
            if bars is None or not np.isfinite(r['Atr_5']):
                continue
            fds = cf_eval.slice_funding(fd, bars)
            gp_ = cf_eval.gp_v2(r['Atr_5'], float(bars['open'].iloc[0]))
            pv5 = pv_spike_for_window(m1, bars, PVC5)
            for cn, scfg in CHAINS.items():
                try:
                    o = simulate_grid_engine(
                        bars, gp_, cap=1000.0, leverage=GEARING / MAX_RATE,
                        fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                        stop_cfg=dict(scfg), funding_df=fds, pv_spike_df=pv5,
                        neutral_init=False, active_stop_mode='pv',
                        pv_pnl_thr=_STOP['pv_pnl_thr'])
                    res[cn].append(float(o['pnl_ratio']) - r['pool_s'])
                except Exception:
                    pass
            if len(m1m) > 150:
                m1m.clear()
                fdm.clear()
        for cn in CHAINS:
            a = np.array(res[cn]) * 1e4
            n = len(a)
            pooled[cn] += list(a)
            print('%-7s %-4s %+7.1f/t%+5.2f  n=%d'
                  % (wn, cn, a.mean(), a.mean() / (a.std() / np.sqrt(n)), n), flush=True)
    for cn, v in pooled.items():
        x = np.array(v)
        print('★六窗合并 RSP111×%s: alpha mean%+.1fbp t%+.2f n=%d'
              % (cn, x.mean(), x.mean() / (x.std() / np.sqrt(len(x))), len(x)), flush=True)


if __name__ == '__main__':
    main()
