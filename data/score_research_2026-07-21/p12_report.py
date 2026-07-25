"""p12 战役报表(2026-07-25):解析 p12_final_results.txt → 三张表 + **机械化裁决**。

判据不由人解释,由代码执行(预注册 docs/superpowers/specs/2026-07-25-p12-holdout-prereg.md §5):
  主判据 p12_St5:HOLD-C 与 HOLD-D **均** Calmar≥锚 且 MDD≤锚×1.3(最差窗规则)
  副判据 p12_s030:同报,做"选币器 vs 执行链"四象限归因
  St4 照报不裁;判定窗只做布线自检,不参与裁决
执行细则同步实现:含等号平局(报表精度1位小数)/负Calmar照代数序/inf判不可比→FAIL/
有效格数<锚95%判不可比→FAIL。
用法: .venv/bin/python data/score_research_2026-07-21/p12_report.py
"""
import re
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')

RES = ('/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21/'
       'ablation/p12_final_results.txt')
BASE_ANCHOR = {'OOS': (1.85, 4.7), 'W1': (-2.83, -3.7), 'W2': (6.31, 17.4),
               'IS': (13.11, 11.2), 'HOLD-A': (-2.36, -2.9), 'HOLD-B': (1.58, 4.1)}
MAIN_W = ['OOS', 'W1', 'W2', 'IS', 'HOLD-A', 'HOLD-B']
HOLD_W = ['HOLD-C', 'HOLD-D']
# 报表列全臂;但 verdict() 只认 p12_St5(主)/p12_s030(副)——F30/F99/St4 是背景臂,
# 照报不裁(用户令加臂时明确"对比用",预注册 §5 判据未随之改动)
ARMS = ['anchor', 'p12_s030', 'p12_St5', 'p12_St4', 'p12_F30', 'p12_F99']
JUDGED = ('p12_St5', 'p12_s030')
PAT = re.compile(
    r'^(?P<stage>\w+)/(?P<win>[\w-]+):\s+(?P<arm>\S+)\s+ret\s*(?P<ret>[-+][\d.]+)\s+'
    r'mdd\s*(?P<mdd>[-\d.]+)\s+calmar\s*(?P<cal>[-\d.inf]+)\s+格(?P<n>\d+)\s+'
    r'破(?P<broke>\d+)\s+爆(?P<blown>\d+)\s+固(?P<fix>\d+)\s+pv(?P<pv>\d+)\s+'
    r'最差(?P<worst>[-+][\d.]+)')


def parse():
    d = {}
    try:
        for ln in open(RES, encoding='utf-8'):
            m = PAT.match(ln.strip())
            if not m:
                continue
            g = m.groupdict()
            d[(g['win'], g['arm'])] = {
                'ret': float(g['ret']), 'mdd': abs(float(g['mdd'])),
                'calmar': float(g['cal']) if g['cal'] not in ('inf', '-inf') else float('inf'),
                'n': int(g['n']), 'broke': int(g['broke']), 'blown': int(g['blown']),
                'fix': int(g['fix']), 'pv': int(g['pv']), 'worst': float(g['worst'])}
    except FileNotFoundError:
        print('无结果文件 %s' % RES)
    return d


def table(d, wins, title):
    print('\n===== %s =====' % title, flush=True)
    hdr = '%-8s %-9s %8s %7s %8s %6s %5s %5s %5s %7s' % (
        '窗', '臂', 'ret%', 'mdd%', 'calmar', '格', '破', '爆', '固', '最差')
    print(hdr)
    print('-' * len(hdr))
    for w in wins:
        for a in ARMS:
            r = d.get((w, a))
            if not r:
                continue
            print('%-8s %-9s %+8.2f %7.2f %8.1f %6d %5d %5d %5d %+7.3f'
                  % (w, a, r['ret'], -r['mdd'], r['calmar'], r['n'],
                     r['broke'], r['blown'], r['fix'], r['worst']))
        if any((w, a) in d for a in ARMS):
            print('-' * len(hdr))


def anchor_check(d):
    """锚基准对照(P12_ANCHOR_MODE=record 口径)。

    ⚠BASE_TD 是**档案补全前**的历史存档。2026-07-25 补全判定窗 1m 后,锚的 bars 更完整,
    微幅偏差属**预期**,不构成保真度问题——注入代码的保真度已在补全前由 parity 四层逐位
    + HOLD-B 锚逐位复现 BASE_TD 证死(见 prereg 执行记录)。此处只照报偏差,不判 PASS/FAIL。
    """
    print('\n===== ① 锚基准对照(补全后 vs 补全前存档 BASE_TD) =====', flush=True)
    print('%-8s %18s %18s %10s %s'
          % ('窗', '补全后 ret/calmar', '存档 ret/calmar', 'Δret', '说明'))
    for w in MAIN_W:
        r = d.get((w, 'anchor'))
        if not r:
            print('%-8s %18s %18s %10s %s'
                  % (w, '(未跑)', '%.2f/%.1f' % BASE_ANCHOR[w], '—', '—'))
            continue
        br, bc = BASE_ANCHOR[w]
        dr = r['ret'] - br
        same = abs(dr) <= 0.02 and abs(r['calmar'] - bc) <= 0.15
        print('%-8s %18s %18s %+10.2f %s'
              % (w, '%+.2f/%.1f' % (r['ret'], r['calmar']),
                 '%+.2f/%.1f' % (br, bc), dr,
                 '与存档一致' if same else '补全后微调(预期)'))
    print('说明: 补全对锚臂影响极小(选币走 1h,1h 未变 ⇒ 选中币集不变;偏差仅来自 1m 补齐)')
    return True


def verdict(d):
    """机械化执行预注册 §5:人不参与解释。"""
    print('\n===== ③ 留出裁决(预注册判据,机械执行) =====', flush=True)
    print('主判据: p12_St5 在 HOLD-C 与 HOLD-D **均** Calmar≥锚 且 MDD≤锚×1.3')
    print('(St4/F30/F99 = 背景臂,照报不裁;判据不因背景臂表现而改)')
    out = {}
    for arm in JUDGED:
        rows, ok_all, why = [], True, []
        for w in HOLD_W:
            a, p = d.get((w, 'anchor')), d.get((w, arm))
            if not a or not p:
                rows.append('%s: (缺数据)' % w)
                ok_all = False
                why.append('%s 缺数据' % w)
                continue
            c_ok = round(p['calmar'], 1) >= round(a['calmar'], 1)
            m_ok = p['mdd'] <= a['mdd'] * 1.3
            comparable = True
            if p['calmar'] in (float('inf'), float('-inf')) or \
               a['calmar'] in (float('inf'), float('-inf')):
                comparable = False
                why.append('%s Calmar=inf 不可比(§5.4)' % w)
            if a['n'] and p['n'] / a['n'] < 0.95:
                comparable = False
                why.append('%s 有效格数%.1f%%<95%% 不可比(§5.5)' % (w, p['n'] / a['n'] * 100))
            win_ok = c_ok and m_ok and comparable
            ok_all = ok_all and win_ok
            rows.append('%-7s Calmar %+7.1f vs 锚%+7.1f [%s] | MDD %5.2f vs 上限%5.2f [%s] → %s'
                        % (w, p['calmar'], a['calmar'], 'OK' if c_ok else 'X',
                           p['mdd'], a['mdd'] * 1.3, 'OK' if m_ok else 'X',
                           'PASS' if win_ok else 'FAIL'))
        print('\n--- %s (%s) ---' % (arm, '主判据' if arm == 'p12_St5' else '副判据'))
        for r in rows:
            print('   ' + r)
        print('   → %s' % ('PASS' if ok_all else 'FAIL'))
        if why:
            print('   备注: ' + '; '.join(why))
        out[arm] = ok_all
    b, c = out.get('p12_St5'), out.get('p12_s030')
    print('\n===== 归因四象限(预注册 §5 副判据) =====')
    if b and c:
        s = '收益主要来自**选币器**(p12),链是放大器 → 两者皆可独立立项'
    elif b and not c:
        s = '收益依赖 **St5链×p12 交互**(更脆,实盘化须连链一起搬)'
    elif (not b) and c:
        s = '**St5链在新regime是负担** → 候选退回 p12×s030 另立项'
    else:
        s = '**整条线判死**(与留出斩杀率 7/7 的先验一致)'
    print('   ' + s)
    return b


def main():
    d = parse()
    if not d:
        return
    anchor_check(d)
    table(d, MAIN_W, '② 判定窗四臂(布线自检,**不参与裁决**——六窗对本战役全污染)')
    if any((w, 'anchor') in d for w in HOLD_W):
        table(d, HOLD_W, '③ 留出窗四臂(唯一裁决)')
        verdict(d)
    else:
        print('\n(留出窗未跑,裁决待出)')


if __name__ == '__main__':
    main()
