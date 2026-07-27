"""RSP111 战役报表(2026-07-25):判定段八窗对照 + 主臂选择 + HOLD-E 机械裁决。

**"八窗合计 Calmar" 的定稿算法(写在数据齐之前,不许事后换)**:
  合计 Calmar := 八窗 Calmar 的**算术平均(等权,每窗一票)**。
  选此不选"净值拼接"的理由:①八窗时间不完全连续(HOLD-E 段与 2025-12 下旬是缺口),
  拼接需人为处理断点;②等权每窗一票,避免天数最多的 IS(122天)按样本量主导;
  ③主臂选择只是"挑一个臂进 HOLD-E",非裁决本身,可解释性优先。
  **已知弱点(诚实记录)**:即便等权,IS 窗各臂 Calmar 差异达 20+,而其余窗差异多在 ±3
  以内 ⇒ 主臂事实上仍由 IS 单窗主导。此弱点写在选择之前,不因结果而改。
  同时并陈中位数/最差窗两种汇总**仅作背景**,不参与主臂选择。

HOLD-E 部署门(唯一裁决,预注册 §):主臂 Calmar≥锚 且 MDD≤锚×1.3 且 ret>锚,三条全过。
用法: rsp_report.py
"""
import re
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')

import numpy as np

RES = ('/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21/'
       'ablation/rsp_final_results.txt')
MAIN_W = ['HOLD-B', 'HOLD-D', 'HOLD-A', 'HOLD-C', 'W1', 'W2', 'OOS', 'IS']   # 时间序
ARMS = ['anchor', 'rsp_v2f3', 'rsp_St4', 'rsp_St5', 'rsp_F30', 'rsp_s030']
RSP_ARMS = [a for a in ARMS if a != 'anchor']
BASE_ANCHOR = {'W1': (-2.86, -3.7), 'W2': (6.31, 17.4), 'OOS': (2.06, 5.2),
               'IS': (13.11, 11.2), 'HOLD-A': (-2.33, -2.9), 'HOLD-B': (1.75, 4.5),
               'HOLD-C': (-2.69, -2.9), 'HOLD-D': (-2.46, -3.0)}
PAT = re.compile(
    r'^(?P<stage>[\w-]+)/(?P<win>[\w-]+):\s+(?P<arm>\S+)\s+ret\s*(?P<ret>[-+][\d.]+)\s+'
    r'mdd\s*(?P<mdd>[-\d.]+)\s+calmar\s*(?P<cal>[-\d.inf]+)\s+格(?P<n>\d+)\s+'
    r'破(?P<broke>\d+)\s+爆(?P<blown>\d+)\s+固(?P<fix>\d+)\s+pv(?P<pv>\d+)\s+'
    r'最差(?P<worst>[-+][\d.]+)\s+[\d.]+min\s*\|\s*(?P<exits>.*)$')


def parse():
    d = {}
    for ln in open(RES, encoding='utf-8'):
        m = PAT.match(ln.strip())
        if not m:
            continue
        g = m.groupdict()
        d[(g['stage'], g['win'], g['arm'])] = {
            'ret': float(g['ret']), 'mdd': abs(float(g['mdd'])),
            'calmar': float(g['cal']) if g['cal'] not in ('inf', '-inf') else float('inf'),
            'n': int(g['n']), 'broke': int(g['broke']), 'blown': int(g['blown']),
            'fix': int(g['fix']), 'pv': int(g['pv']), 'worst': float(g['worst']),
            'exits': g['exits']}
    return d


def anchor_check(d):
    print('\n===== ① 锚复现(判定八窗 vs 上一战补全后存档) =====')
    print('%-8s %18s %18s  %s' % ('窗', '本战役', '存档', '判定'))
    ok_all = True
    for w in MAIN_W:
        r = d.get(('MAIN', w, 'anchor'))
        if not r:
            print('%-8s %18s %18s  %s' % (w, '(未跑)', '%+.2f/%.1f' % BASE_ANCHOR[w], '—'))
            ok_all = False
            continue
        br, bc = BASE_ANCHOR[w]
        ok = abs(r['ret'] - br) <= 0.02 and abs(r['calmar'] - bc) <= 0.15
        ok_all = ok_all and ok
        print('%-8s %18s %18s  %s' % (w, '%+.2f/%.1f' % (r['ret'], r['calmar']),
                                      '%+.2f/%.1f' % (br, bc),
                                      'OK 逐位' if ok else '!!! 不复现'))
    print('锚门: %s' % ('PASS(八窗逐位复现)' if ok_all else '未全过'))
    return ok_all


def table(d, stage, wins, title):
    print('\n===== %s =====' % title)
    hdr = '%-8s %-10s %8s %7s %8s %6s %5s %5s %5s %7s' % (
        '窗', '臂', 'ret%', 'mdd%', 'calmar', '格', '破', '爆', '固', '最差')
    print(hdr)
    print('-' * len(hdr))
    for w in wins:
        for a in ARMS:
            r = d.get((stage, w, a))
            if not r:
                continue
            print('%-8s %-10s %+8.2f %7.2f %8.1f %6d %5d %5d %5d %+7.3f'
                  % (w, a, r['ret'], -r['mdd'], r['calmar'], r['n'],
                     r['broke'], r['blown'], r['fix'], r['worst']))
        if any((stage, w, a) in d for a in ARMS):
            print('-' * len(hdr))


def aggregate(d):
    """八窗合计(定稿:算术平均;中位/最差窗仅背景)。"""
    print('\n===== ② 八窗合计(合计 Calmar := 算术平均,等权每窗一票) =====')
    have = [w for w in MAIN_W if ('MAIN', w, 'anchor') in d]
    print('参与窗(%d): %s' % (len(have), ', '.join(have)))
    hdr = '%-10s %10s %10s %10s %10s %8s' % ('臂', '合计Calmar', '(中位)', '(最差窗)',
                                             '平均ret%', '胜锚窗数')
    print(hdr)
    print('-' * len(hdr))
    agg = {}
    for a in ARMS:
        cs = [d[('MAIN', w, a)]['calmar'] for w in have if ('MAIN', w, a) in d]
        rs = [d[('MAIN', w, a)]['ret'] for w in have if ('MAIN', w, a) in d]
        if len(cs) < len(have):
            continue
        wins_vs = sum(1 for w in have
                      if d[('MAIN', w, a)]['calmar'] > d[('MAIN', w, 'anchor')]['calmar'])
        agg[a] = float(np.mean(cs))
        print('%-10s %10.2f %10.2f %10.2f %10.2f %8s'
              % (a, np.mean(cs), np.median(cs), min(cs), np.mean(rs),
                 '%d/%d' % (wins_vs, len(have)) if a != 'anchor' else '—'))
    return agg, have


def pick_main_arm(agg):
    """主臂 = 判定段八窗合计 Calmar 最高的 RSP 臂(规则先声明,此处仅应用)。"""
    cand = {a: v for a, v in agg.items() if a in RSP_ARMS}
    if not cand:
        return None
    best = max(cand, key=lambda a: cand[a])
    print('\n===== ③ 主臂选择(规则:八窗合计 Calmar 最高的 RSP 臂) =====')
    for a in sorted(cand, key=lambda x: -cand[x]):
        print('   %-10s %8.2f %s' % (a, cand[a], '← 主臂' if a == best else ''))
    print('   ⚠已知弱点(预注册已记):IS 单窗各臂 Calmar 差异 20+,其余窗多在 ±3 内,'
          '主臂事实上由 IS 主导。')
    return best


def verdict(d, main_arm):
    """HOLD-E 部署门:主臂 Calmar≥锚 且 MDD≤锚×1.3 且 ret>锚(三条全过)。"""
    a = d.get(('HOLD-E', 'HOLD-E', 'anchor'))
    if not a:
        print('\n(HOLD-E 未跑,终裁待出)')
        return None
    print('\n===== ④ HOLD-E 终裁(唯一裁决,机械执行) =====')
    print('部署门: 主臂 Calmar≥锚 且 MDD≤锚×1.3 且 ret>锚 —— 三条全过')
    print('锚: ret%+.2f mdd%.2f calmar%.1f | 上限 MDD %.2f'
          % (a['ret'], a['mdd'], a['calmar'], a['mdd'] * 1.3))
    out = {}
    for arm in RSP_ARMS:
        p = d.get(('HOLD-E', 'HOLD-E', arm))
        if not p:
            continue
        c_ok = round(p['calmar'], 1) >= round(a['calmar'], 1)
        m_ok = p['mdd'] <= a['mdd'] * 1.3
        r_ok = p['ret'] > a['ret']
        comparable = (p['calmar'] not in (float('inf'), float('-inf'))
                      and a['n'] and p['n'] / a['n'] >= 0.95)
        ok = c_ok and m_ok and r_ok and comparable
        out[arm] = ok
        mark = ' ← 主臂' if arm == main_arm else '  (参考)'
        print('  %-10s C%+7.1f[%s] MDD%6.2f[%s] ret%+7.2f[%s] %s→ %s%s'
              % (arm, p['calmar'], 'OK' if c_ok else 'X',
                 p['mdd'], 'OK' if m_ok else 'X',
                 p['ret'], 'OK' if r_ok else 'X',
                 '' if comparable else '不可比 ', 'PASS' if ok else 'FAIL', mark))
    if main_arm in out:
        print('\n**部署门裁决(仅主臂 %s 作数): %s**'
              % (main_arm, 'PASS → 可进部署评估' if out[main_arm] else 'FAIL → 判死'))
    return out


def main():
    d = parse()
    if not d:
        print('无结果')
        return
    anchor_check(d)
    table(d, 'MAIN', MAIN_W, '判定段八窗六臂(**全污染,只做估计不做裁决**)')
    agg, have = aggregate(d)
    main_arm = pick_main_arm(agg) if len(have) == len(MAIN_W) else None
    if main_arm is None:
        print('\n(八窗未齐,主臂待定——规则已声明,数据齐后应用)')
    if ('HOLD-E', 'HOLD-E', 'anchor') in d:
        table(d, 'HOLD-E', ['HOLD-E'], 'HOLD-E 六臂(唯一裁决窗)')
        verdict(d, main_arm)


if __name__ == '__main__':
    main()
