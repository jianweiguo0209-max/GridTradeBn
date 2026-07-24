"""IS 终裁三件套(2026-07-25,等 cf_IS.parquet 落地后运行):
① 预注册主检验: p12_gross×s030原链 IS 全窗 alpha(mean/med/t/胜率)+四未见窗合并t
② 参考读数(用户令): {St4,St5} 链变体对 IS p12top 格重放,与 s030原链/现役 并陈
   绝对值(收益/MDD/Calmar,伪组合1/12) + alpha
③ E0 转化率悬案: IS 全窗 E0 alpha vs s030 alpha
用法: p12_is_final.py
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
PVC5 = {'mult': 5, 'n': 100, 'period': '15min'}
VAR = {'St4': dict(stop_loss=0.04, **TR, **FR),
       'St5': dict(stop_loss=0.05, **TR, **FR),
       'F30': dict(stop_loss=0.05, **TR, fundingRate_stop_loss=0.003),
       'F99': dict(stop_loss=0.05, **TR, fundingRate_stop_loss=1.0)}
# 此前未见窗读数(合并t用): (mean_bp, n, std_bp)
PRIOR = {'HOLD-A': (21.6, 220, 188.3), 'W2': (-11.6, 243, 247.0), 'OOS': (13.8, 247, 216.9)}


def curve(rs):
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rs:
        eq *= (1 + r / 12)
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    return eq - 1, mdd


def main():
    cf = pd.read_parquet(f'{RD}/ablation/cf_IS.parquet')
    lab = pd.read_parquet(f'{RD}/sc_labels_IS.parquet')[['rt', 'symbol', 'cross1']].rename(
        columns={'cross1': 'p12'})
    lab['run_time'] = lab['rt'] + pd.Timedelta(hours=12)
    cf = cf.merge(lab[['run_time', 'symbol', 'p12']], on=['run_time', 'symbol'], how='left')
    picks, cur_rows = [], []
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
            picks.append({'run_time': rt, 'symbol': r['symbol'], 'Atr_5': r['Atr_5'],
                          'pool_s': gp_['pnl_s030'].mean(), 'pool_e0': gp_['pnl_e0'].mean(),
                          'e0': r['pnl_e0'], 's030': r['pnl_s030'],
                          'cur_s': pk['pnl_s030'].mean()})
    P = pd.DataFrame(picks)
    n = len(P)
    # ① 主检验
    a = (P['s030'] - P['pool_s']) * 1e4
    m, sd = a.mean(), a.std()
    t_is = m / (sd / np.sqrt(n))
    print('① 主检验 p12×s030原链 IS: mean%+.1f med%+.1f t%+.2f 胜率%.2f n=%d'
          % (m, a.median(), t_is, (a > 0).mean(), n), flush=True)
    tot_num = m * n + sum(mm * nn for mm, nn in
                          [(v[0], v[1]) for v in PRIOR.values()])
    tot_n = n + sum(v[1] for v in PRIOR.values())
    pool_mean = tot_num / tot_n
    pool_var = (sd**2 * (n - 1) + sum(v[2]**2 * (v[1] - 1) for v in PRIOR.values())) \
        / (tot_n - 4)
    pool_t = pool_mean / (np.sqrt(pool_var) / np.sqrt(tot_n))
    pos = sum(1 for v in PRIOR.values() if v[0] > 0) + (1 if m > 0 else 0)
    print('★ 四未见窗合并: mean%+.1fbp t%+.2f 正窗%d/4 | 判据(t≥2且≥3正): %s'
          % (pool_mean, pool_t, pos,
             'PASS' if (pool_t >= 2 and pos >= 3) else 'FAIL'), flush=True)
    # ③ E0 转化率
    ae = (P['e0'] - P['pool_e0']) * 1e4
    print('③ E0悬案: alphaE0 mean%+.1f med%+.1f t%+.2f | s030转化率=%.0f%%'
          % (ae.mean(), ae.median(), ae.mean() / (ae.std() / np.sqrt(n)),
             100 * m / ae.mean() if abs(ae.mean()) > 1e-9 else float('nan')), flush=True)
    # ② 参考读数: St4/St5 重放
    cache = ParquetCache(V.default_cache_root())
    m1m, fdm = {}, {}
    out = {k: [] for k in VAR}
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
            for k in VAR:
                out[k].append(np.nan)
            continue
        fds = cf_eval.slice_funding(fd, bars)
        gp = cf_eval.gp_v2(r['Atr_5'], float(bars['open'].iloc[0]))
        pv_df = pv_spike_for_window(m1, bars, PVC5)
        for k, scfg in VAR.items():
            try:
                res = simulate_grid_engine(
                    bars, gp, cap=1000.0, leverage=GEARING / MAX_RATE,
                    fee=FEE_MAKER, c_rate_taker=FEE_TAKER, max_rate=MAX_RATE,
                    stop_cfg=dict(scfg), funding_df=fds, pv_spike_df=pv_df,
                    neutral_init=False, active_stop_mode='pv',
                    pv_pnl_thr=_STOP['pv_pnl_thr'])
                out[k].append(float(res['pnl_ratio']))
            except Exception:
                out[k].append(np.nan)
        if len(m1m) > 150:
            m1m.clear()
            fdm.clear()
    for k in VAR:
        P[k] = out[k]
    days = (P['run_time'].max() - P['run_time'].min()).days or 1
    print('② IS 参考读数(绝对值+alpha):', flush=True)
    for k, col in (('现役×s030', 'cur_s'), ('p12×s030原链', 's030'),
                   ('p12×St4', 'St4'), ('p12×St5', 'St5'),
                   ('p12×F30', 'F30'), ('p12×F99', 'F99')):
        rs = P.groupby('run_time')[col].mean().sort_index().values
        rs = rs[np.isfinite(rs)]
        t_, mm_ = curve(rs)
        cal = t_ * 365 / days / mm_ if mm_ > 1e-9 else 0
        al = (P[col] - P['pool_s']).dropna() * 1e4
        print('  %-11s 收益%+7.2f%% MDD%5.2f%% C%+6.1f | alpha%+7.1fbp t%+5.2f'
              % (k, t_ * 100, mm_ * 100, cal, al.mean(),
                 al.mean() / (al.std() / np.sqrt(len(al)))), flush=True)
    P.to_parquet(f'{RD}/ablation/p12_is_final.parquet')


if __name__ == '__main__':
    main()
