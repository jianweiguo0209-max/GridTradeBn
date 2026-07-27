"""eff1×紧固损探针(2026-07-26,救回方案v2之③,仅两格):
画像依据: eff1选中格=燃料top10%×低mae(逆行天然小),v2宽固损系全劣于s030(战役全样本),
唯一未试方向=比s030更紧。臂: S20(固2%)/S25(固2.5%),其余逐项=s030现役链
(trailing k0.3/floor2%,pv mult3/thr-1%,funding 0.0015)。参照=事实库存档 pnl_s030(固3%)。
六窗探索口径(HOLD-C/D/E无本方事实库,略)。
用法: eff1_tightstop_probe.py
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

LAB = {w: f'{RD}/sc_labels_{w}.parquet' for w in ('W1', 'W2', 'OOS', 'IS')}
LAB.update({w: f'{RD}/ablation/hold_labels_{w}.parquet' for w in ('HOLD-A', 'HOLD-B')})
WD = {'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
      'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
      'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30')}
BASE = {'trailing_k': _STOP['trailing_k'], 'trailing_floor': _STOP['trailing_floor'],
        'fundingRate_stop_loss': _STOP['fundingRate_stop_loss']}
CHAINS = {'S20': dict(stop_loss=0.020, **BASE), 'S25': dict(stop_loss=0.025, **BASE)}


def main():
    cache = ParquetCache(V.default_cache_root())
    pooled = {c: [] for c in list(CHAINS) + ['s030']}
    print('窗      链   alpha(bp)/t (基线=池s030均值)  n')
    for wn in ('W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B'):
        cf = pd.read_parquet(f'{RD}/ablation/cf_{wn}.parquet')
        lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1', 'mae']]
        lab['eff'] = lab['cross1'] / (1.0 + 100.0 * lab['mae'])
        lab['run_time'] = lab['rt'] + pd.Timedelta(hours=12)
        cf = cf.merge(lab[['run_time', 'symbol', 'eff']], on=['run_time', 'symbol'],
                      how='left')
        picks = []
        for rt, g in cf.groupby('run_time'):
            gp = g[g['in_pool']]
            av = gp.dropna(subset=['eff'])
            if len(av) < 30:
                continue
            p = av.nlargest(1, 'eff').iloc[0]
            picks.append({'run_time': rt, 'symbol': p['symbol'],
                          'Atr_5': float(p['Atr_5']), 'pool_s': gp['pnl_s030'].mean(),
                          's030': p['pnl_s030']})
        m1lo = pd.Timestamp(WD[wn][0]) - pd.Timedelta(days=2)
        m1hi = pd.Timestamp(WD[wn][1]) + pd.Timedelta(days=2)
        m1m, fdm = {}, {}
        res = {c: [] for c in CHAINS}
        s030col = []
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
            pv = pv_spike_for_window(m1, bars, cf_eval.PV_CFG)
            for c, scfg in CHAINS.items():
                try:
                    o = simulate_grid_engine(
                        bars, gp_, cap=1000.0, leverage=GEARING / MAX_RATE,
                        fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                        stop_cfg=dict(scfg), funding_df=fds, pv_spike_df=pv,
                        neutral_init=False, active_stop_mode='pv',
                        pv_pnl_thr=_STOP['pv_pnl_thr'])
                    res[c].append(float(o['pnl_ratio']) - r['pool_s'])
                except Exception:
                    pass
            s030col.append(r['s030'] - r['pool_s'])
            if len(m1m) > 150:
                m1m.clear()
                fdm.clear()
        for c in ['s030'] + list(CHAINS):
            a = np.array(s030col if c == 's030' else res[c]) * 1e4
            pooled[c] += list(a)
            print('%-7s %-4s %+7.1f/t%+5.2f  n=%d'
                  % (wn, c, a.mean(), a.mean() / (a.std() / np.sqrt(len(a))), len(a)),
                  flush=True)
    for c, v in pooled.items():
        x = np.array(v)
        print('★六窗合并 eff1×%s: %+.1fbp t%+.2f n=%d'
              % (c, x.mean(), x.mean() / (x.std() / np.sqrt(len(x))), len(x)), flush=True)


if __name__ == '__main__':
    main()
