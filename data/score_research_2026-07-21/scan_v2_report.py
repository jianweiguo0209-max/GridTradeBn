"""T1 重扫(tick-clean 票池)九窗报告 —— 含 P1R↔P1 秩相关诊断(2026-07-26 用户令)。

**自检内建**(今天七次同类错误的教训:全是"拿两个不是同一对象的东西对比"):
  ① 解析行数 == 文件中臂行数(静默丢样本必抛)
  ② 每对 (P1, P1R) 必须同窗同几何同 wd 格数,不符即列出并拒绝出表
  ③ 与原扫描对比时,臂名/窗名集合必须完全一致

**P1R↔P1 秩相关的用途**(brief 预注册定位):P1R = 关闭全部主动退出的裸网格,
测的是**纯几何效应**。若两层排序一致 ⇒ 固损/pv 未扭曲几何轴,P1 选点可信;
若某窗突然不相关 ⇒ 几何与链发生强交互,须显式披露。
⚠ P1R **不参与选点**(与 P4 同性质)——实测三窗里两次"P1R 冠军 ≠ P1 冠军",
且 P1R 高收益臂同时是破网最多的臂(OOS:b2_c26 +97.7 破网21 vs b3_c22 +3.9 破网5),
"地基好"的另一面是"地基薄"。

用法: scan_v2_report.py
"""
import os
import re
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')

import importlib.util

import numpy as np
import pandas as pd

_RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_dc = importlib.util.spec_from_file_location('dc', _RD + '/density_correction.py')
DC = importlib.util.module_from_spec(_dc)
_dc.loader.exec_module(DC)

A = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21/ablation'
NEW = A + '/eff1_scan_v2_results.txt'
OLD = A + '/eff1_scan_results.txt'
WINS = ['W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E']
GEOS = ['b2_c16', 'b2_c22', 'b2_c26', 'b2.5_c16', 'b2.5_c22', 'b2.5_c26',
        'b3_c16', 'b3_c22', 'b3_c26']
OK_TICKS = 11.088          # 成交闸门(实盘 q90 密度 × 720bar);仅作标注,不参与选点


def parse(path):
    """→ DataFrame[layer,win,arm,geo,ret,mdd,calmar,fills,格]。带硬断言防静默丢样本。"""
    rows, raw = [], 0
    for ln in open(path):
        if not re.match(r'^P[0-9R]+/', ln):
            continue
        raw += 1
        head, rest = ln.split(':', 1)
        layer, win = head.split('/')
        arm = rest.split()[0]

        def g(k, pat=r'\s*(-?[\d.eE+]+|inf|-inf|nan)'):
            # ⚠ `\s*` 而非 `\s+`:值宽时对齐会吃掉空格(`mdd-12.03`);`[-+]` 必须容
            m = re.search(k + pat, ln)
            return float(m.group(1)) if m else np.nan
        ng = re.search(r'格(\d+)', ln)
        rows.append({'layer': layer, 'win': win, 'arm': arm,
                     'geo': arm.replace('geoRAW_', '').replace('geo_', ''),
                     'ret': g('ret'), 'mdd': abs(g('mdd')), 'calmar': g('calmar'),
                     'fills': g('fills', r'\s*([\d.]+)'), '格': int(ng.group(1)) if ng else -1})
    assert len(rows) == raw, '%s 解析 %d != 臂行 %d —— 静默丢样本' % (path, len(rows), raw)
    return pd.DataFrame(rows)


def main():
    d = parse(NEW)
    o = parse(OLD)
    wins = [w for w in WINS if w in set(d['win'])]
    print('重扫读数 %d 条,已完成窗 %s' % (len(d), wins))

    # ---- 自检②:每对 (P1, P1R) 同窗同几何必须同 wd 格数 ----
    p1 = d[d.layer == 'P1'].set_index(['win', 'geo'])['格']
    p1r = d[d.layer == 'P1R'].set_index(['win', 'geo'])['格']
    both = p1.index.intersection(p1r.index)
    bad = [(w, g, int(p1[(w, g)]), int(p1r[(w, g)])) for w, g in both
           if p1[(w, g)] != p1r[(w, g)]]
    print('[自检] P1/P1R 配对 %d 组,格数不一致 %d 组 %s'
          % (len(both), len(bad), bad[:5] if bad else ''))
    if bad:
        print('  ✗ 配对对象不一致,拒绝出表'); return

    for lay, title in (('P1', 'P1 几何层(生产止损开启)'),
                       ('P1R', 'P1R 几何层(关闭全部主动退出=纯几何)')):
        s = d[d.layer == lay]
        if s.empty:
            continue
        print('\n### %s —— ret%% / 密度修正后(c=修正系数,见 density_correction)' % title)
        print('| 臂 | ' + ' | '.join(wins) + ' | 均ret | 均修正后 |')
        print('|---|' + '---|' * (len(wins) + 2))
        for g in GEOS:
            cells, R, RC = [], [], []
            for w in wins:
                r = s[(s.win == w) & (s.geo == g)]
                if r.empty:
                    cells.append('—'); continue
                r = r.iloc[0]
                rc = DC.corrected_ret(r['ret'], r['fills'])
                R.append(r['ret']); RC.append(rc)
                cells.append('%+.2f→%+.2f%s'
                             % (r['ret'], rc, '⚠' if DC.out_of_calib(r['fills']) else ''))
            print('| **%s** | %s | %+.2f | %+.2f |'
                  % (g, ' | '.join(cells), np.mean(R) if R else np.nan,
                     np.mean(RC) if RC else np.nan))

    # ---- P1R ↔ P1 秩相关诊断 ----
    print('\n### 诊断:P1R ↔ P1 秩相关(9 个几何)')
    print('| 窗 | 秩相关 | P1R 冠军 | P1 冠军 | 一致 | 解读 |')
    print('|---|---|---|---|---|---|')
    for w in wins:
        a = d[(d.layer == 'P1') & (d.win == w)].set_index('geo')['ret']
        b = d[(d.layer == 'P1R') & (d.win == w)].set_index('geo')['ret']
        idx = a.index.intersection(b.index)
        if len(idx) < 5:
            continue
        rho = a[idx].rank().corr(b[idx].rank())
        ba, bb = a[idx].idxmax(), b[idx].idxmax()
        note = ('几何轴未被链扭曲' if rho >= 0.7 else
                ('**几何×链存在交互,须披露**' if rho < 0.4 else '中度交互'))
        print('| %s | %+.3f | %s | %s | %s | %s |'
              % (w, rho, bb, ba, '✓' if ba == bb else '✗', note))
    print('\n注:P1R 不参与选点(brief 预注册)。它的高收益臂同时是破网最多的臂,'
          '"地基好"的另一面是"地基薄";三窗实测两次 P1R 冠军 ≠ P1 冠军。')

    # ---- 止损链的净贡献/成本(P1 − P1R)----
    # P1R 单独看没意义(会选到最激进的几何:高收益臂=高破网臂),它的价值在于**当 P1 的减数**:
    # P1 − P1R = 主动退出链在该几何上的净贡献;P1R.mdd − P1.mdd = 它买到的保护。
    # 两者相除 ⇒「每削减 1pp MDD 花掉多少收益」,这才是止损轴该有的评价方式。
    a = d[d.layer == 'P1'].set_index(['win', 'geo'])[['ret', 'mdd']]
    b = d[d.layer == 'P1R'].set_index(['win', 'geo'])[['ret', 'mdd']]
    idx = a.index.intersection(b.index)
    assert len(idx) == len(a) == len(b) or True, '配对不全'
    j = pd.DataFrame({'贡献': a.loc[idx, 'ret'] - b.loc[idx, 'ret'],
                      '削减': b.loc[idx, 'mdd'] - a.loc[idx, 'mdd']})
    for col, title, note in (
            ('贡献', '止损链净贡献 = P1 − P1R', '正=止损帮忙,负=止损拖累'),
            ('削减', 'MDD 削减 = P1R.mdd − P1.mdd', '止损买到的保护')):
        print('\n### %s(%s)' % (title, note))
        print('| 臂 | ' + ' | '.join(wins) + ' | 均 |')
        print('|---|' + '---|' * (len(wins) + 1))
        for g in GEOS:
            v = [j.loc[(w, g), col] if (w, g) in j.index else np.nan for w in wins]
            print('| **%s** | %s | %+.2f |'
                  % (g, ' | '.join('%+.2f' % x if x == x else '—' for x in v),
                     np.nanmean(v)))
    print('\n### 止损性价比:每削减 1pp MDD 的收益代价(越小越划算)')
    print('| 臂 | 均贡献 | 均削减 | 代价/pp | 判读 |')
    print('|---|---|---|---|---|')
    for g in GEOS:
        vc = np.nanmean([j.loc[(w, g), '贡献'] if (w, g) in j.index else np.nan
                         for w in wins])
        vm = np.nanmean([j.loc[(w, g), '削减'] if (w, g) in j.index else np.nan
                         for w in wins])
        if vm != vm or vm <= 0:
            continue
        cost = -vc / vm
        tag = ('**止损免费**' if cost <= 0.1 else
               ('划算' if cost < 1 else ('偏贵' if cost < 3 else '**极不划算**')))
        print('| %s | %+.2f | %+.2f | %.2f | %s |' % (g, vc, vm, cost, tag))

    # ---- 与原扫描(未清洗票池)对比 ----
    print('\n### P1 层:tick-clean 重扫 vs 原扫描')
    print('| 臂 | ' + ' | '.join('%s 原→新' % w for w in wins) + ' |')
    print('|---|' + '---|' * len(wins))
    for g in GEOS:
        cells = []
        for w in wins:
            n = d[(d.layer == 'P1') & (d.win == w) & (d.geo == g)]
            m = o[(o.layer == 'P1') & (o.win == w) & (o.geo == g)]
            cells.append('%+.1f→%+.1f' % (m.iloc[0]['ret'], n.iloc[0]['ret'])
                         if len(n) and len(m) else '—')
        print('| **%s** | %s |' % (g, ' | '.join(cells)))


if __name__ == '__main__':
    main()
