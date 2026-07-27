"""p12 变体×定制链全谱探针(2026-07-25,用户令"不用s030,用St4/5+F99验证"):
选币器 {gross, paired, r7f(≤50%)} × 链 {St4, St5, F99} × 窗 {W2, HOLD-C, HOLD-D}。
池=战役 p12_pool_*(top-1 已证与我方序列重合0.99-1.00);标签=对应窗标签;stride-5;
(rt,sym) 级模拟缓存跨链共享(pv5 一次三链复用)。F99 带 carry 定性警示,探索口径。
用法: p12_variant_chains_probe.py
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

WD = {'W2': ('2025-10-15', '2025-12-14'),
      'HOLD-C': ('2025-04-01', '2025-05-31'), 'HOLD-D': ('2024-12-01', '2025-01-31')}
LAB = {'W2': f'{RD}/sc_labels_W2.parquet',
       'HOLD-C': f'{RD}/ablation/hold_labels_HOLD-C.parquet',
       'HOLD-D': f'{RD}/ablation/hold_labels_HOLD-D.parquet'}
L101 = np.log(1.01)
TR = {'trailing_k': 0.15, 'trailing_floor': 0.01}
CHAINS = {
    'St4': dict(stop_loss=0.04, **TR, fundingRate_stop_loss=_STOP['fundingRate_stop_loss']),
    'St5': dict(stop_loss=0.05, **TR, fundingRate_stop_loss=_STOP['fundingRate_stop_loss']),
    'F99': dict(stop_loss=0.05, **TR, fundingRate_stop_loss=1.0),
}
PVC5 = {'mult': 5, 'n': 100, 'period': '15min'}


def curve(rs):
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rs:
        eq *= (1 + r / 12)
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    return eq - 1, mdd


def main():
    cache = ParquetCache(V.default_cache_root())
    for wn, (w0s, w1s) in WD.items():
        pool = pd.read_parquet(f'{RD}/ablation/p12_pool_{wn}.parquet')[
            ['rt', 'symbol', 'Atr_5']]
        lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1', 'drift']].rename(
            columns={'rt': 'lab_rt'})
        lab['rt'] = lab['lab_rt'] + pd.Timedelta(hours=12)
        lab['paired'] = (lab['cross1'] - np.log1p(lab['drift']) / L101) / 2
        pool = pool.merge(lab[['rt', 'symbol', 'cross1', 'paired']].rename(
            columns={'cross1': 'p12'}), on=['rt', 'symbol'], how='left')
        rts = sorted(pool['rt'].unique())[::5]
        pool = pool[pool['rt'].isin(rts)]
        w0 = pd.Timestamp(w0s) - pd.Timedelta(days=9)
        w1 = pd.Timestamp(w1s) + pd.Timedelta(days=2)
        r7map = {}
        for sym in pool['symbol'].unique():
            h = cache.read_days_range('1h', sym, w0.strftime('%Y-%m-%d'),
                                      w1.strftime('%Y-%m-%d'))
            if h is None or len(h) < 24:
                continue
            r7map[sym] = (h['candle_begin_time'].values.astype('datetime64[ns]')
                          .astype(np.int64), h['close'].astype(float).values)

        def r7(sym, rt):
            v = r7map.get(sym)
            if v is None:
                return np.nan
            ts, c = v
            t = np.int64(pd.Timestamp(rt).value)
            i = np.searchsorted(ts, t) - 1
            j = np.searchsorted(ts, t - 168 * 3600 * 10**9) - 1
            if i < 0 or j < 0 or i <= j:
                return np.nan
            return c[i] / c[j] - 1

        picks = {'gross': [], 'paired': [], 'r7f': []}
        for rt, g in pool.groupby('rt'):
            av = g[np.isfinite(g['p12'])]
            if len(av) < 30:
                continue
            picks['gross'].append(av.nlargest(1, 'p12').iloc[0])
            picks['paired'].append(av.nlargest(1, 'paired').iloc[0])
            rr = {s: r7(s, rt) for s in av['symbol']}
            ok = av[av['symbol'].map(lambda s: np.isfinite(rr[s]) and rr[s] <= 0.5)]
            if len(ok):
                picks['r7f'].append(ok.nlargest(1, 'p12').iloc[0])

        simcache = {}
        m1m, fdm = {}, {}
        m1lo = pd.Timestamp(w0s) - pd.Timedelta(days=2)
        m1hi = pd.Timestamp(w1s) + pd.Timedelta(days=2)

        def sim(rt, sym, atr5):
            key = (rt, sym)
            if key in simcache:
                return simcache[key]
            m1 = m1m.get(sym)
            if m1 is None:
                m1 = cache.read_days_range('1m', sym, m1lo.strftime('%Y-%m-%d'),
                                           m1hi.strftime('%Y-%m-%d'))
                m1m[sym] = m1
            fd = fdm.get(sym)
            if fd is None:
                fd = cache.read_all_days('funding', sym)
                fdm[sym] = fd
            bars = cf_eval.prep_window(m1, pd.Timestamp(rt))
            if bars is None or not np.isfinite(atr5):
                simcache[key] = None
                return None
            fds = cf_eval.slice_funding(fd, bars)
            gp = cf_eval.gp_v2(atr5, float(bars['open'].iloc[0]))
            pv5 = pv_spike_for_window(m1, bars, PVC5)
            out = {}
            for cn, scfg in CHAINS.items():
                try:
                    res = simulate_grid_engine(
                        bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
                        fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                        stop_cfg=dict(scfg), funding_df=fds, pv_spike_df=pv5,
                        neutral_init=False, active_stop_mode='pv',
                        pv_pnl_thr=_STOP['pv_pnl_thr'])
                    out[cn] = float(res['pnl_ratio'])
                except Exception:
                    out[cn] = np.nan
            simcache[key] = out
            return out

        print(f'===== {wn} =====', flush=True)
        for sel, rows in picks.items():
            res = {cn: [] for cn in CHAINS}
            for r in rows:
                o = sim(r['rt'], r['symbol'], float(r['Atr_5']))
                if o is None:
                    continue
                for cn in CHAINS:
                    if np.isfinite(o[cn]):
                        res[cn].append(o[cn])
            for cn, rs in res.items():
                t, m = curve(rs)
                a = np.array(rs) * 1e4
                print('  %-6s ×%-4s 轮=%d 格均%+7.1fbp t%+5.2f | 伪组合 %+6.2f%% MDD%5.2f%%'
                      % (sel, cn, len(rs), a.mean(),
                         a.mean() / (a.std() / np.sqrt(len(a))), t * 100, m * 100),
                      flush=True)


if __name__ == '__main__':
    main()
