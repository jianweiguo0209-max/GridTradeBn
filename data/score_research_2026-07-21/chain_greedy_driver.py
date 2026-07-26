"""链轴 **束搜索(beam search, B=3)** · 无人值守驱动器 K1→K2→K3→K4。

**为什么是束不是贪心**(用户令 2026-07-27「每个最优不一定是一组值,可以最多留两到三组」):
贪心(B=1)是局部最优且**路径依赖** —— 若某段选出的值恰好排除了下一轴的最优点,后面回不去。
而交互已被证实真实存在(`pv_mult=5` 在 b2_c26 上的效果)。束宽 3 把这个风险大幅降低,
代价是三段总耗时 1.6h → 2.4h、累计搜索预算 25 → **39 点**。

**选点规则(写死于任何一段出数之前,防事后合理化)**:
  主键   = 九窗合计 ret(密度修正后;IS 由两段相加近似整窗)
  硬约束 = **无任何窗** MDD > 生产现值同窗 MDD × 1.3
  每段保留满足硬约束者中主键最高的 **前 3 个**作为下一段的基座束
  若某段全部越 MDD 上限 ⇒ 束保持不变(该轴不动,最小改动原则)
  每段同报三口径:九窗合计 / 判定四窗 / 留出五窗
  —— 今天已两次见"判定封神、留出团灭",单看合计会瞎

**性质:全程知识扫描,不产生部署结论。** 九窗已全部消费;终点若要部署,必须先写死预注册,
再在 HOLD-F/JUL26 上看一次。**本驱动器不碰终审窗**(硬断言 + 窗集校验)。
⚠ 最终有 **3 个候选**进裁决 ⇒ **多重比较**,预注册判据须据此加严。

**并行**:每段十单元(八窗 + IS 两段)分两作业,窗集不相交,各 BT_WORKERS=3。
每单元约 5 分钟是单线程(blocked_rts + tick表 + 预热),两作业错开正好互补。

用法: chain_greedy_driver.py     (可重入:每段落盘,重启自动续)
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')

import numpy as np

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
A = RD + '/ablation'
PY = '/Users/thomaschang/Projects/GridTradeBi/.venv/bin/python'
_d = importlib.util.spec_from_file_location('dc', RD + '/density_correction.py')
DC = importlib.util.module_from_spec(_d)
_d.loader.exec_module(DC)

LOG = A + '/greedy_driver.log'
BEAM = 3
J4 = ['W1', 'W2', 'OOS', 'IS']
H5 = ['HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E']
SPLIT_A = 'W2,OOS,HOLD-A,HOLD-B,IS-1'
SPLIT_B = 'W1,HOLD-C,HOLD-D,HOLD-E,IS-2'
STAGES = [('K2', {'pv_thr': [-0.005, -0.02]}),
          ('K3', {'trailing_floor': [0.01, 0.04], 'trailing_k': [0.15]}),
          ('K4', {'funding_stop': [0.003, 1.0]})]
assert not ({'HOLD-F', 'JUL26'} & (set(SPLIT_A.split(',')) | set(SPLIT_B.split(',')))), \
    '处女终审窗禁入'


def log(s):
    line = '[%s] %s' % (time.strftime('%H:%M:%S'), s)
    open(LOG, 'a').write(line + '\n')
    print(line, flush=True)


def parse(path, stage):
    out, raw = {}, 0
    if not os.path.exists(path):
        return out
    for ln in open(path):
        if not ln.startswith(stage + '/'):
            continue
        raw += 1
        h, r = ln.split(':', 1)
        w, arm = h.split('/')[1], r.split()[0]

        def g(k, p=r'\s*(-?[\d.eE+]+)'):
            m = re.search(k + p, ln)
            return float(m.group(1)) if m else np.nan
        f = g('fills', r'\s*([\d.]+)')
        out[(w, arm)] = dict(ret=DC.corrected_ret(g('ret'), f), mdd=abs(g('mdd')))
    assert len(out) == raw, '%s 解析 %d != 行 %d(静默丢样本)' % (path, len(out), raw)
    return out


def prod_mdd():
    p = parse(A + '/eff1_scan_v2_results.txt', 'P1')
    return {w: p.get((w, 'geo_b3_c16'), {}).get('mdd', np.nan) for w in J4 + H5}


def score(res, arm):
    """→ (九窗, 判定4, 留出5, 越限窗列表) 或 None(该臂未跑全)。"""
    pm = prod_mdd()
    r, over = {}, []
    for w in J4 + H5:
        if w == 'IS':
            a, b = res.get(('IS-1', arm)), res.get(('IS-2', arm))
            if not a or not b:
                return None
            r[w], md = a['ret'] + b['ret'], max(a['mdd'], b['mdd'])
        else:
            k = res.get((w, arm))
            if not k:
                return None
            r[w], md = k['ret'], k['mdd']
        if md > pm[w] * 1.3:
            over.append(w)
    return (sum(r.values()), sum(r[w] for w in J4), sum(r[w] for w in H5), over)


def report_and_beam(stage, res, name2cfg):
    arms = sorted({a for _w, a in res})
    rows = []
    log('%s 结果(%d 臂):' % (stage, len(arms)))
    for a in arms:
        sc = score(res, a)
        if sc is None:
            log('   %-14s (未跑全,跳过)' % a); continue
        n9, n4, n5, over = sc
        log('   %-14s 九窗%+8.2f  判定4%+8.2f  留出5%+8.2f  MDD越限%s'
            % (a, n9, n4, n5, over if over else '无'))
        if not over:
            rows.append((n9, a))
    if not rows:
        log('%s 全部越 MDD 上限 ⇒ 束不变(该轴不动)' % stage); return None
    rows.sort(reverse=True)
    keep = rows[:BEAM]
    log('%s ⇒ 保留前 %d:' % (stage, len(keep)))
    for n9, a in keep:
        log('     %-14s 九窗%+8.2f  %s' % (a, n9, json.dumps(name2cfg[a], sort_keys=True)))
    return [(a, name2cfg[a]) for _n, a in keep]


def run_stage(stage, configs, budget):
    env = dict(os.environ, K_STAGE=stage, K_BUDGET=str(budget), BT_WORKERS='3',
               OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
               K_CONFIGS=json.dumps([[n, c] for n, c in configs]))
    procs = []
    for tag, wins in (('a', SPLIT_A), ('b', SPLIT_B)):
        f = open('%s/%s_%s.log' % (A, stage.lower(), tag), 'a')
        procs.append(subprocess.Popen([PY, '-u', RD + '/eff1_chain_scan.py'],
                                      env=dict(env, K_WINS=wins), stdout=f, stderr=f))
        time.sleep(25)
    log('%s 两作业已起 pid=%s(%d 臂 × 10 单元)'
        % (stage, [p.pid for p in procs], len(configs)))
    for p in procs:
        p.wait()
    log('%s 两作业退出 码=%s' % (stage, [p.returncode for p in procs]))


def main():
    log('=' * 74)
    log('束搜索驱动器启动 B=%d。规则:主键=九窗合计ret(密度修正);硬约束=无窗 MDD>现值×1.3;' % BEAM)
    log('  每段保留满足约束者前 %d 个;全越限⇒该轴不动。**不碰 HOLD-F/JUL26**。' % BEAM)
    while subprocess.run(['pgrep', '-f', 'eff1_k1_pv5stop'],
                         capture_output=True).returncode == 0:
        time.sleep(60)
    log('K1 已结束')
    res = parse(A + '/eff1_k1_results.txt', 'K1')
    n2c = {}
    for _w, a in res:
        m = re.match(r's([\d.]+)_m5$', a)
        if m:
            n2c[a] = {'pv_mult': 5, 'stop_loss': float(m.group(1)) / 100.0}
    beam = report_and_beam('K1', res, n2c)
    if not beam:
        log('K1 无可用点 ⇒ 退回生产现值,流水线终止'); return
    budget = 18
    for stage, axis in STAGES:
        cfgs, n2c = [], {}
        for i, (bn, bc) in enumerate(beam):
            tag = chr(ord('a') + i)
            nm = 'BASE_%s' % tag
            cfgs.append((nm, dict(bc))); n2c[nm] = dict(bc)
            for k, vals in axis.items():
                for v in vals:
                    ov = dict(bc); ov[k] = v
                    sfx = ('%g' % (v * 1000)) if abs(v) < 0.1 else ('%g' % v)
                    nm2 = '%s_%s%s' % (tag, k.replace('_', '')[:5], sfx)
                    cfgs.append((nm2, ov)); n2c[nm2] = ov
        budget += sum(len(v) for v in axis.values()) * len(beam)
        log('--- %s 开跑:%d 臂(束 %d × 轴 %d + 基座门),累计预算=%d ---'
            % (stage, len(cfgs), len(beam), sum(len(v) for v in axis.values()), budget))
        run_stage(stage, cfgs, budget)
        nb = report_and_beam(stage, parse('%s/eff1_%s_results.txt' % (A, stage.lower()),
                                          stage), n2c)
        if nb:
            beam = nb
        else:
            log('%s 束不变' % stage)
    log('=' * 74)
    log('束搜索完成。累计搜索预算 = %d 点。最终 %d 个候选:' % (budget, len(beam)))
    for n, c in beam:
        log('   %-14s %s' % (n, json.dumps(c, sort_keys=True)))
    log('**下一步需人工**:写死预注册(冻结候选/判据/失败含义;**3 候选 ⇒ 多重比较须加严**)')
    log('  经审阅后才可碰 HOLD-F/JUL26 —— 最后一块处女地,只能花一次。')
    json.dump([{'name': n, 'config': c} for n, c in beam],
              open(A + '/greedy_final_beam.json', 'w'), indent=2, ensure_ascii=False)


if __name__ == '__main__':
    main()
