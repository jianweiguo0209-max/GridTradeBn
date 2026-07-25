"""消融格战役 HOLD-E 机械裁决(2026-07-26)。

判据由代码执行,人不参与解释。预注册 docs/superpowers/specs/2026-07-26-ep2-holde-prereg.md
(冻结 commit c61c7ac):
  主臂 = EP2_s030(判定段八窗合计 Calmar 最高,规则先声明后应用,结果已冻结)
  部署门(唯一裁决,三条**全过**): Calmar≥锚 且 MDD≤锚×1.3 且 ret>锚
  其余 17 臂同跑同报,**只作参考,不参与裁决**,不得因参考臂过门而改立候选。
执行细则同步实现:条①含等号(1位小数)、条③严格大于、负Calmar按代数序、
inf或有效格数<锚95%判不可比=FAIL、贴线(0.2C/0.3pp)提示须重跑POOL同源复核。
用法: rsp2_verdict.py
"""
import re
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')

import numpy as np

RES = ('/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21/'
       'ablation/rsp2_final_results.txt')
MAIN_ARM = 'EP2_s030'                       # 冻结主臂(预注册 §3)
SELS = ['D_ESP', 'D_REP', 'EP2']
CHAINS = ['s030', 'v2f3', 'St4', 'St5', 'F30', 'F99']
ARMS = ['anchor'] + ['%s_%s' % (s, c) for s in SELS for c in CHAINS]
W8 = ['HOLD-B', 'HOLD-D', 'HOLD-A', 'HOLD-C', 'W1', 'W2', 'OOS', 'IS']
PAT = re.compile(
    r'^(?P<stage>[\w-]+)/(?P<win>[\w-]+):\s+(?P<arm>\S+)\s+ret\s*(?P<ret>[-+][\d.]+)\s+'
    r'mdd\s*(?P<mdd>[-\d.]+)\s+calmar\s*(?P<cal>[-\d.inf]+)\s+格(?P<n>\d+)')


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
            'n': int(g['n'])}
    return d


def main():
    d = parse()
    a = d.get(('HOLD-E', 'HOLD-E', 'anchor'))
    if not a:
        print('(HOLD-E 锚未出,裁决待跑)')
        return
    print('===== HOLD-E 六链×三选币器 全表(唯一裁决窗) =====')
    print('%-12s %9s %8s %9s %7s' % ('臂', 'ret%', 'mdd%', 'calmar', '格'))
    print('-' * 50)
    for arm in ARMS:
        r = d.get(('HOLD-E', 'HOLD-E', arm))
        if not r:
            continue
        mark = ' ← 主臂' if arm == MAIN_ARM else ''
        print('%-12s %+9.2f %8.2f %9.1f %7d%s'
              % (arm, r['ret'], -r['mdd'], r['calmar'], r['n'], mark))
    print('\n===== 部署门(机械执行,仅主臂 %s 作数) =====' % MAIN_ARM)
    print('锚: ret%+.2f mdd%.2f calmar%.1f | MDD上限%.2f'
          % (a['ret'], a['mdd'], a['calmar'], a['mdd'] * 1.3))
    p = d.get(('HOLD-E', 'HOLD-E', MAIN_ARM))
    if not p:
        print('主臂未出')
        return
    c_ok = round(p['calmar'], 1) >= round(a['calmar'], 1)
    m_ok = p['mdd'] <= a['mdd'] * 1.3
    r_ok = p['ret'] > a['ret']
    comparable = (p['calmar'] not in (float('inf'), float('-inf'))
                  and a['n'] and p['n'] / a['n'] >= 0.95)
    ok = c_ok and m_ok and r_ok and comparable
    print('  ① Calmar %+7.1f ≥ 锚 %+7.1f ? %s' % (p['calmar'], a['calmar'],
                                                  'OK' if c_ok else 'X'))
    print('  ② MDD    %7.2f ≤ 上限%7.2f ? %s' % (p['mdd'], a['mdd'] * 1.3,
                                                 'OK' if m_ok else 'X'))
    print('  ③ ret    %+7.2f > 锚 %+7.2f ? %s' % (p['ret'], a['ret'],
                                                  'OK' if r_ok else 'X'))
    if not comparable:
        print('  ⚠不可比(inf 或有效格数 %.1f%% < 95%%)' % (100.0 * p['n'] / max(a['n'], 1)))
    print('\n**部署门裁决: %s**' % ('PASS → 进入部署评估' if ok else 'FAIL → 判死'))
    # 贴线提示(预注册 §5.4)
    gaps = [('Calmar', abs(p['calmar'] - a['calmar']), 0.2),
            ('MDD', abs(p['mdd'] - a['mdd'] * 1.3), 0.3),
            ('ret', abs(p['ret'] - a['ret']), 0.3)]
    tight = [g for g, v, t in gaps if v <= t]
    if tight:
        print('⚠贴线维度 %s(差距≤阈值)→ 预注册 §5.4:须重跑 POOL 同源复核后再定'
              % ', '.join(tight))
    print('\n--- 参考臂(不参与裁决) ---')
    for arm in ARMS[1:]:
        if arm == MAIN_ARM:
            continue
        r = d.get(('HOLD-E', 'HOLD-E', arm))
        if not r:
            continue
        o = (round(r['calmar'], 1) >= round(a['calmar'], 1)
             and r['mdd'] <= a['mdd'] * 1.3 and r['ret'] > a['ret'])
        print('  %-12s C%+7.1f MDD%6.2f ret%+7.2f  %s'
              % (arm, r['calmar'], r['mdd'], r['ret'], '三条全过' if o else '—'))
    # F99 carry 标签提示(brief 修订三)
    print('\n注:F99 系按 carry 标签解读——超额若集中于深负费率窗且复苏窗回吐,'
          '按资金费 carry 读数,不按选币边读数。主臂非 F99,不影响裁决。')


if __name__ == '__main__':
    main()
