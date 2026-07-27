"""J1 选点 —— 严格执行预注册 2026-07-26-geo-chain-joint §3(规则出数前已冻结,此处只执行)。

规则(逐条,前一条不满足即不进下一条):
 1. 二维平台:候选在 (band, count, pv_thr, pv_mult) 上的 4-邻域均值须落全部 54 点前 25%;
    孤峰淘汰不论绝对值。**S1 角点约束:邻居数 < 3 的点不得当选。**
 2. 双基线优越:判定四窗合计须同时优于 (a) 现值 b3_c16×s030,
    (b) T1 重扫按**判定四窗合计**定的最佳单轴改动版(R3:不得用五/九窗均值)。
 3. 最差窗规则:判定四窗最差一窗 ret ≥ 现值同窗 − 3pp。
 4. 主序 = ret / MDD 分列,**不用 Calmar**;收益用密度修正后值。
 5. 无点同时满足 1~3 ⇒ 就地终止,保持现值,不进留出(HOLD-F/JUL26 不消费)。

用法: j1_select.py
"""
import importlib.util
import re
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')

import numpy as np
import pandas as pd

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
A = RD + '/ablation'
_d = importlib.util.spec_from_file_location('dc', RD + '/density_correction.py')
DC = importlib.util.module_from_spec(_d)
_d.loader.exec_module(DC)

J = ['W1', 'W2', 'OOS', 'IS']
BANDS, COUNTS = [2, 2.5, 3], [16, 22, 26]
THRS, MULTS = [-5, -10, -20], [3, 5]          # 臂名里的 pv_thr×1000
PROD = 'b3_c16_pv-10_m3'                       # 现值


def load(path, pref, pat):
    rows, raw = [], 0
    for ln in open(path):
        if not ln.startswith(pref):
            continue
        raw += 1
        head, rest = ln.split(':', 1)
        w = head.split('/')[1]
        arm = rest.split()[0]

        def g(k, p=r'\s*(-?[\d.eE+]+)'):
            m = re.search(k + p, ln)
            return float(m.group(1)) if m else np.nan
        f = g('fills', r'\s*([\d.]+)')
        rows.append({'w': w, 'arm': arm, 'ret': DC.corrected_ret(g('ret'), f),
                     'raw_ret': g('ret'), 'mdd': abs(g('mdd')), 'fills': f})
    assert len(rows) == raw, '%s 解析 %d != 行 %d' % (path, len(rows), raw)
    d = pd.DataFrame(rows)
    return d[d.w.isin(J)]


def key(a):
    m = re.match(r'b([\d.]+)_c(\d+)_pv(-?\d+)_m(\d+)', a)
    return (float(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def neighbours(k):
    b, c, t, m = k
    out = []
    for i, seq in ((0, BANDS), (1, COUNTS), (2, THRS), (3, MULTS)):
        j = seq.index(k[i])
        for jj in (j - 1, j + 1):
            if 0 <= jj < len(seq):
                v = list(k); v[i] = seq[jj]; out.append(tuple(v))
    return out


def main():
    j1 = load(A + '/eff1_j1_results.txt', 'J1/', None)
    t1 = load(A + '/eff1_scan_v2_results.txt', 'P1/', None)
    agg = j1.groupby('arm')['ret'].sum()
    assert len(agg) == 54, '组合数 %d != 54' % len(agg)
    prod_w = j1[j1.arm == PROD].set_index('w')['ret']
    prod_sum = prod_w.sum()
    # 基线(b):T1 重扫按判定四窗合计定的最佳**单轴**改动版
    t1s = t1.groupby('arm')['ret'].sum().drop('geo_b3_c16', errors='ignore')
    best_single, best_single_v = t1s.idxmax(), t1s.max()
    print('== 基线 ==')
    print('  (a) 现值 %s 判定四窗合计 = %+.2f' % (PROD, prod_sum))
    print('  (b) T1 最佳单轴(按判定四窗合计,R3) = %s  %+.2f' % (best_single, best_single_v))
    q75 = None
    # 规则1:4-邻域均值 + 角点约束
    K = {a: key(a) for a in agg.index}
    inv = {v: k for k, v in K.items()}
    nb_mean, nb_n = {}, {}
    for a, k in K.items():
        ns = [inv[n] for n in neighbours(k) if n in inv]
        nb_n[a] = len(ns)
        nb_mean[a] = np.mean([agg[x] for x in ns]) if ns else np.nan
    nm = pd.Series(nb_mean)
    q75 = nm.quantile(0.75)
    print('\n== 规则1 二维平台(4-邻域均值须 ≥ 前25%%分位 = %+.2f)+ S1 角点(邻居≥3)==' % q75)
    r1 = [a for a in agg.index if nb_n[a] >= 3 and nm[a] >= q75]
    print('  54 点中:邻居<3 被排除 %d 个;通过平台判据 %d 个'
          % (sum(1 for a in K if nb_n[a] < 3), len(r1)))
    tab = pd.DataFrame({'合计ret': agg, '邻域均值': nm, '邻居数': pd.Series(nb_n)})
    print(tab.sort_values('合计ret', ascending=False).head(12)
          .to_string(float_format=lambda x: '%.2f' % x))
    if not r1:
        print('\n⇒ **规则1 零通过 → §3.5 就地终止,保持现值**'); return
    # 规则2:双基线
    print('\n== 规则2 双基线优越(须同时 > %+.2f 且 > %+.2f)==' % (prod_sum, best_single_v))
    r2 = [a for a in r1 if agg[a] > prod_sum and agg[a] > best_single_v]
    for a in sorted(r1, key=lambda x: -agg[x])[:8]:
        print('  %-22s 合计%+9.2f  vs现值 %s  vs单轴 %s'
              % (a, agg[a], '✓' if agg[a] > prod_sum else '✗',
                 '✓' if agg[a] > best_single_v else '✗'))
    if not r2:
        print('\n⇒ **规则2 零通过 → §3.5 就地终止,保持现值,不进留出**')
        print('   (HOLD-A~E 与 HOLD-F/JUL26 均不消费)')
        return
    # 规则3:最差窗
    print('\n== 规则3 最差窗 ≥ 现值同窗 − 3pp ==')
    r3 = []
    for a in r2:
        s = j1[j1.arm == a].set_index('w')['ret']
        d = (s[J] - prod_w[J]).min()
        ok = d >= -3.0
        r3.append(a) if ok else None
        print('  %-22s 最差窗差 %+.2f  %s' % (a, d, '✓' if ok else '✗'))
    if not r3:
        print('\n⇒ **规则3 零通过 → §3.5 就地终止,保持现值**'); return
    print('\n== 终点(规则1~3 全过,按合计 ret 降序)==')
    for a in sorted(r3, key=lambda x: -agg[x]):
        s = j1[j1.arm == a]
        print('  %-22s 合计%+9.2f  均MDD %.2f  均fills %.1f' %
              (a, agg[a], s['mdd'].mean(), s['fills'].mean()))
    print('\n⇒ 唯一终点 = **%s**' % sorted(r3, key=lambda x: -agg[x])[0])


if __name__ == '__main__':
    main()
