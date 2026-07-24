"""p12_cross1 → E0 bp 口径测量(2026-07-24 用户令"先把预热过的数据跑一下p12_cross1"):
对已完成的 cf_<win>.parquet(全池逐轮 E0 事实),把"按 p12_cross1(过去12h已实现燃料)
选 top-K"当假想选币器,与现役选中/池均值/池中位在 bp 口径对照;并读 s030 口径
(燃料型选币过不过得了止损链)。
回顾性测量(工具合法用途,spec §6);任何选币改动照走三道门+留出。
用法: p12_probe.py [WIN ...]   默认扫所有已完成窗
"""
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


def probe(wn):
    p = '%s/ablation/cf_%s.parquet' % (RD, wn)
    if not os.path.exists(p):
        return None
    cf = pd.read_parquet(p)
    lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1']]
    lab = lab.rename(columns={'cross1': 'p12_cross1'})
    # 标签行 rt=T 描述 [T,T+12h) → 选币轮 R 的"过去12h已实现" = 标签行 T=R−12h
    lab['run_time'] = lab['rt'] + pd.Timedelta(hours=12)
    cf = cf.merge(lab[['run_time', 'symbol', 'p12_cross1']],
                  on=['run_time', 'symbol'], how='left')
    rows = []
    for rt, g in cf.groupby('run_time'):
        gp = g[g['in_pool']]
        pk = g[g['picked']]
        if gp.empty or pk.empty:
            continue
        k = len(pk)
        avail = gp[np.isfinite(gp['p12_cross1'])]
        if len(avail) < 30:              # 标签覆盖不足的轮不计
            continue
        p12top = avail.nlargest(k, 'p12_cross1')
        true_top = gp.nlargest(k, 'pnl_e0')
        rows.append({
            'rt': rt, 'k': k, 'cov': len(avail) / len(gp),
            'p12_e0': p12top['pnl_e0'].mean(),
            'p12_s030': p12top['pnl_s030'].mean(),
            'pick_e0': pk['pnl_e0'].mean(),
            'pick_s030': pk['pnl_s030'].mean(),
            'pool_e0': gp['pnl_e0'].mean(),
            'pool_med': gp['pnl_e0'].median(),
            'p12_hit': len(set(p12top['symbol']) & set(true_top['symbol'])),
            'pick_hit': len(set(pk['symbol']) & set(true_top['symbol'])),
        })
    d = pd.DataFrame(rows)
    if d.empty:
        return None
    return {
        '窗': wn, '轮数': len(d),
        'p12标签覆盖': round(float(d['cov'].mean()), 2),
        'p12top_E0均值(bp)': round(d['p12_e0'].mean() * 1e4, 1),
        '现役选中E0均值(bp)': round(d['pick_e0'].mean() * 1e4, 1),
        '池均值(bp)': round(d['pool_e0'].mean() * 1e4, 1),
        '池中位(bp)': round(d['pool_med'].mean() * 1e4, 1),
        'p12_alpha_E0(bp)': round((d['p12_e0'] - d['pool_e0']).mean() * 1e4, 1),
        '现役alpha_E0(bp)': round((d['pick_e0'] - d['pool_e0']).mean() * 1e4, 1),
        'p12top_s030均值(bp)': round(d['p12_s030'].mean() * 1e4, 1),
        '现役选中s030均值(bp)': round(d['pick_s030'].mean() * 1e4, 1),
        'p12捕获率': round(float(d['p12_hit'].sum() / d['k'].sum()), 3),
        '现役捕获率': round(float(d['pick_hit'].sum() / d['k'].sum()), 3),
    }


def main(wins):
    out = []
    for w in wins:
        r = probe(w)
        if r:
            out.append(r)
    t = pd.DataFrame(out).set_index('窗').T
    print(t.to_string(), flush=True)
    md = ['| 指标 | ' + ' | '.join(str(c) for c in t.columns) + ' |',
          '|---|' + '---|' * len(t.columns)]
    for idx, r in t.iterrows():
        md.append('| %s | ' % idx + ' | '.join(
            ('%g' % v) if isinstance(v, float) else str(v) for v in r.values) + ' |')
    with open('%s/ablation/p12_probe.md' % RD, 'w') as f:
        f.write('\n'.join(md) + '\n')


if __name__ == '__main__':
    main(sys.argv[1:] or WINS)
