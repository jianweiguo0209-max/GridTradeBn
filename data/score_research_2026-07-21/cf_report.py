"""P1 成绩单(spec §3):六窗 选币alpha/捕获率/遗憾/止损链税/池体温+诊断。
排序类指标一律主口径 pnl_e0;s030 并读。A/B 比较只算分歧格(共同选中精确抵消)。
用法: cf_report.py            # 有 cf_<win>.parquet 的窗全出,写 ablation/cf_results.txt
"""
import json
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')

import numpy as np
import pandas as pd

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
WINS = ['W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B']
LAB = {w: '%s/sc_labels_%s.parquet' % (RD, w) for w in ('W1', 'W2', 'OOS', 'IS')}
LAB.update({w: '%s/ablation/hold_labels_%s.parquet' % (RD, w)
            for w in ('HOLD-A', 'HOLD-B')})


def per_round_metrics(cf):
    """逐轮:alpha(选中−池均)/hit(∩真实topK)/regret(topK−选中)/池体温。主口径 pnl_e0。"""
    out = []
    for rt, g in cf.groupby('run_time'):
        gp = g[g['in_pool']]
        pk = g[g['picked']]
        if gp.empty or pk.empty:
            continue
        k = len(pk)
        top = gp.nlargest(k, 'pnl_e0')
        out.append({'rt': rt, 'k': k,
                    'alpha_e0': pk['pnl_e0'].mean() - gp['pnl_e0'].mean(),
                    'alpha_s030': pk['pnl_s030'].mean() - gp['pnl_s030'].mean(),
                    'hit': len(set(pk['symbol']) & set(top['symbol'])),
                    'regret': top['pnl_e0'].mean() - pk['pnl_e0'].mean(),
                    'pool_med': gp['pnl_e0'].median(),
                    'pool_top': top['pnl_e0'].mean()})
    return pd.DataFrame(out)


def aggregate(d, cf):
    """窗级聚合(bp 口径)。"""
    pk = cf[cf['picked']]
    pl = cf[cf['in_pool']]
    return {'rounds': len(d),
            'alpha_e0_bp': d['alpha_e0'].mean() * 1e4,
            'alpha_s030_bp': d['alpha_s030'].mean() * 1e4,
            'capture': d['hit'].sum() / d['k'].sum(),
            'regret_bp': d['regret'].mean() * 1e4,
            'pool_med_bp': d['pool_med'].mean() * 1e4,
            'pool_top_bp': d['pool_top'].mean() * 1e4,
            'tax_picks_bp': (pk['pnl_e0'] - pk['pnl_s030']).mean() * 1e4,
            'tax_pool_bp': (pl['pnl_e0'] - pl['pnl_s030']).mean() * 1e4,
            'picks_outside_pool': int((cf['picked'] & ~cf['in_pool']).sum())}


def diagnostics(cf, lab):
    """选中桶 vs 池的燃料/毒药 z、汇率(z_drift/z_cross1,平衡线0.54)、calib 分(bp)。"""
    j = cf.merge(lab.rename(columns={'rt': 'run_time'}),
                 on=['run_time', 'symbol'], how='left')
    pool = j[j['in_pool']].copy()
    for c in ('cross1', 'drift'):
        g = pool.groupby('run_time')[c]
        pool['z_' + c] = (pool[c] - g.transform('mean')) \
            / g.transform('std').replace(0, np.nan)
    pk = pool[pool['picked']]
    zc, zd = float(pk['z_cross1'].mean()), float(pk['z_drift'].mean())
    w = json.load(open('%s/ablation/score_eval_weights.json' % RD))['weights_bp_per_sigma']
    rate = zd / zc if np.isfinite(zc) and abs(zc) > 1e-9 else np.nan
    return {'z_cross1': zc, 'z_drift': zd, 'rate': rate,
            'calib_bp': w['cross1'] * zc + w['drift'] * zd}


def ab_compare(cf, picks_a, picks_b):
    """A/B 选币器配对比较(spec §3):只算分歧格 E0 差。picks_*: DataFrame[run_time,symbol]。"""
    a = set(map(tuple, picks_a[['run_time', 'symbol']].values))
    b = set(map(tuple, picks_b[['run_time', 'symbol']].values))
    px = cf.set_index(['run_time', 'symbol'])['pnl_e0']
    va = np.array([px[t] for t in a - b if t in px.index])
    vb = np.array([px[t] for t in b - a if t in px.index])
    if not len(va) or not len(vb):
        return {'n_disjoint': 0, 'mean_diff_bp': np.nan, 'se_bp': np.nan}
    diff = va.mean() - vb.mean()
    se = np.sqrt(va.var(ddof=1) / len(va) + vb.var(ddof=1) / len(vb)) \
        if len(va) > 1 and len(vb) > 1 else np.nan
    return {'n_disjoint': len(va) + len(vb), 'mean_diff_bp': diff * 1e4,
            'se_bp': se * 1e4 if np.isfinite(se) else np.nan}


def main():
    lines = ['win     rounds alpha_e0 capture regret pool_med pool_top tax_pk/pl '
             'alpha_s030 汇率 calib 池外']
    for wn in WINS:
        p = '%s/ablation/cf_%s.parquet' % (RD, wn)
        if not os.path.exists(p):
            continue
        cf = pd.read_parquet(p)
        d = per_round_metrics(cf)
        a = aggregate(d, cf)
        dg = diagnostics(cf, pd.read_parquet(LAB[wn]))
        lines.append('%-7s %5d %+7.1fbp %5.2f %+6.1fbp %+7.1fbp %+7.1fbp '
                     '%+4.1f/%+4.1f %+7.1fbp %5.2f %+6.1fbp %3d'
                     % (wn, a['rounds'], a['alpha_e0_bp'], a['capture'],
                        a['regret_bp'], a['pool_med_bp'], a['pool_top_bp'],
                        a['tax_picks_bp'], a['tax_pool_bp'], a['alpha_s030_bp'],
                        dg['rate'], dg['calib_bp'], a['picks_outside_pool']))
    txt = '\n'.join(lines)
    print(txt, flush=True)
    with open('%s/ablation/cf_results.txt' % RD, 'w') as f:
        f.write(txt + '\n')


if __name__ == '__main__':
    main()
