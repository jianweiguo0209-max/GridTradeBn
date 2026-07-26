"""【B】Calmar 幂律缺陷:换不受污染的主序,历史战役排名会不会翻盘?(2026-07-26,用户令作参考)

**缺陷**(见 memory calmar-primary-metric-power-law-flaw):
    ann = (1+ret)^(365/days) − 1 ;  calmar = ann / mdd
窗越短指数越大。判定窗多为 2 个月 ⇒ 指数 6.19 ⇒ 收益翻倍 Calmar 涨 73 倍,
MDD 要恶化 73 倍才抵得过 ⇒ **名为风险调整,实为带 6.19 次幂的收益排序**。

**替代主序**(把指数化换成线性化,其余口径不动):
    calmar_log = (365/days)·ln(1+ret) / mdd
它在 log 空间年化,对 ret 仍单调、对 mdd 仍单调,但**不再有幂律放大** ⇒
高收益高回撤臂不再自动碾压低收益低回撤臂。两者排名不同处即幂律真正改变了结论的地方。

对比示例:A(ret+100%, mdd10%) vs B(ret+30%, mdd2%)
  旧: A=2^6.19/0.10=730 > B=1.3^6.19/0.02=247   ⇒ 选 A
  新: A=0.693/0.10=6.93 < B=0.262/0.02=13.1     ⇒ 选 B(翻盘)

**本脚本只读已落盘的战役结果,不重跑任何回测。**
用法: calmar_powerlaw_rerank.py
"""
import os
import re
from collections import defaultdict

import numpy as np
import pandas as pd

AB = ('/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21/ablation')
WD = {'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
      'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
      'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30'),
      'HOLD-C': ('2025-04-01', '2025-05-31'), 'HOLD-D': ('2024-12-01', '2025-01-31'),
      'HOLD-E': ('2025-06-01', '2025-08-14')}
DAYS = {w: (pd.Timestamp(b) - pd.Timestamp(a)).days + 1 for w, (a, b) in WD.items()}
FILES = {'p12战役': 'p12_final_results.txt', 'RSP26臂战役': 'rsp2_final_results.txt',
         'geo几何战役': 'geo_final_results.txt', 'eff1九窗扫描': 'eff1_scan_results.txt'}


def parse(fn):
    rows = []
    for ln in open(os.path.join(AB, fn)):
        m = re.match(r'^(\S+?)/(\S+?):\s+(\S+)\s', ln)
        if not m:
            continue
        stage, win, arm = m.groups()
        if win not in DAYS:
            continue
        g = {}
        for k in ('ret', 'mdd'):
            # 两个必须容的坑(2026-07-26 各踩一次,都是**静默丢样本**):
            #   ① '+' 号:`ret  +1.75` —— 漏了只留下亏损臂
            #   ② `\s*` 而非 `\s+`:值宽时对齐吃掉空格,`mdd-12.03` / `ret+35932.11`
            #      —— 漏了会**恰好丢掉所有高回撤臂与爆炸臂**,即新口径最该惩罚的那批
            mm = re.search(k + r'\s*([-+]?[\d.]+)', ln)
            if mm:
                g[k] = float(mm.group(1)) / 100.0
        if 'ret' not in g or 'mdd' not in g:
            continue
        rows.append({'stage': stage, 'window': win, 'arm': arm,
                     'ret': g['ret'], 'mdd': abs(g['mdd']), 'days': DAYS[win]})
    # 硬断言:解析数必须等于原始臂行数。静默丢样本两次都伪造出了结论,不许再发生。
    raw = sum(1 for ln in open(os.path.join(AB, fn))
              if re.match(r'^\S+?/\S+?:\s+\S+\s', ln)
              and ln.split('/', 1)[1].split(':', 1)[0] in DAYS)
    assert len(rows) == raw, ('%s 解析 %d != 原始臂行 %d —— 有样本被静默丢弃'
                              % (fn, len(rows), raw))
    return pd.DataFrame(rows)


def add_metrics(d):
    k = 365.0 / d['days']
    d = d.copy()
    d['calmar_ann'] = np.where(d['mdd'] > 1e-9,
                               ((1 + d['ret']) ** k - 1) / d['mdd'].clip(1e-9), np.nan)
    d['calmar_log'] = np.where(d['mdd'] > 1e-9,
                               k * np.log1p(d['ret'].clip(-0.999999))
                               / d['mdd'].clip(1e-9), np.nan)
    return d


def main():
    print('窗天数 / 年化指数(365/days):')
    print('  ' + '  '.join('%s=%d(%.2f)' % (w, DAYS[w], 365.0 / DAYS[w])
                           for w in sorted(DAYS, key=lambda x: DAYS[x])))
    flips_all = []
    for label, fn in FILES.items():
        if not os.path.exists(os.path.join(AB, fn)):
            continue
        d = add_metrics(parse(fn)).dropna(subset=['calmar_ann', 'calmar_log'])
        if d.empty:
            continue
        d = d[d['stage'].isin(['MAIN', 'P1', 'P1R', 'P3', 'P5'])] if 'eff1' in label else d
        print('\n' + '=' * 78)
        print('■ %s  (%d 臂-窗, %d 窗, %d 臂)'
              % (label, len(d), d['window'].nunique(), d['arm'].nunique()))
        # ---- 逐窗:冠军是否易主 ----
        chg = 0
        for w, g in d.groupby('window'):
            a = g.loc[g['calmar_ann'].idxmax()]
            b = g.loc[g['calmar_log'].idxmax()]
            same = a['arm'] == b['arm']
            chg += (not same)
            print('  %-7s 旧冠 %-16s (C%9.4g ret%+.3f mdd%.3f) | 新冠 %-16s (C%8.3f ret%+.3f mdd%.3f) %s'
                  % (w, a['arm'], a['calmar_ann'], a['ret'], a['mdd'],
                     b['arm'], b['calmar_log'], b['ret'], b['mdd'],
                     '' if same else '★易主'))
        print('  ⇒ 逐窗冠军易主 %d/%d 窗' % (chg, d['window'].nunique()))
        # ---- 合计口径(战役实际用的:跨窗求和)----
        agg = d.groupby('arm').agg(n=('window', 'size'), Σann=('calmar_ann', 'sum'),
                                   Σlog=('calmar_log', 'sum'),
                                   ret均=('ret', 'mean'), mdd均=('mdd', 'mean'))
        full = agg[agg['n'] == agg['n'].max()]
        if len(full) >= 2:
            ra = full['Σann'].rank(ascending=False)
            rb = full['Σlog'].rank(ascending=False)
            rho = ra.corr(rb, method='pearson')
            top_a, top_b = full['Σann'].idxmax(), full['Σlog'].idxmax()
            print('  合计口径(%d 窗全在的 %d 臂): 旧主臂=%s  新主臂=%s  %s'
                  % (full['n'].iloc[0], len(full), top_a, top_b,
                     '★主臂易主' if top_a != top_b else '(一致)'))
            print('    秩相关(旧Σ vs 新Σ) = %+.3f   最大名次位移 = %d 名'
                  % (rho, int((ra - rb).abs().max())))
            show = full.assign(旧名=ra.astype(int), 新名=rb.astype(int))
            show = show.sort_values('Σlog', ascending=False).head(8)
            print(show[['旧名', '新名', 'Σann', 'Σlog', 'ret均', 'mdd均']]
                  .to_string(float_format=lambda x: '%.4g' % x))
            flips_all.append((label, top_a != top_b, rho))
    print('\n' + '=' * 78)
    print('总结: 主臂易主的战役 %d/%d'
          % (sum(1 for _l, f, _r in flips_all if f), len(flips_all)))
    for l, f, r in flips_all:
        print('  %-14s %s  秩相关%+.3f' % (l, '★易主' if f else '一致', r))


if __name__ == '__main__':
    main()
