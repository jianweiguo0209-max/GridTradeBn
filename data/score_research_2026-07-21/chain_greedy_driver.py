"""链轴贪心坐标上升 · 无人值守驱动器(K1→K2→K3→K4)。

**选点规则(写死于任何一段出数之前,防事后合理化)**:
  主键   = 九窗合计 ret(密度修正后;IS 由两段合并后按整窗 122 天算指标)
  硬约束 = **无任何窗** MDD > 生产现值同窗 MDD × 1.3
  若该轴无点满足硬约束、或最优点不优于基座 ⇒ **该轴不动**(基座原样进下一段,最小改动原则)
  每段同时报三口径:九窗合计 / 判定四窗 / 留出五窗 —— 因为今天已两次见到
  "判定封神、留出团灭",单看合计会瞎。

**性质:全程知识扫描,不产生部署结论。** 九窗已全部消费;终点若要部署,必须先写死预注册,
再在 HOLD-F/JUL26 上看一次。**本驱动器不碰终审窗**(硬断言),终审留给人工定夺。

**并行**:每段十单元(八窗 + IS 两段)分两作业,窗集不相交,各 BT_WORKERS=3。
每单元约 5 分钟是单线程(blocked_rts+tick表+预热),两作业错开正好互补。

用法: chain_greedy_driver.py     (可重入:每段跑完落盘,重启自动续)
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
import pandas as pd

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
A = RD + '/ablation'
PY = '/Users/thomaschang/Projects/GridTradeBi/.venv/bin/python'
_d = importlib.util.spec_from_file_location('dc', RD + '/density_correction.py')
DC = importlib.util.module_from_spec(_d)
_d.loader.exec_module(DC)

LOG = A + '/greedy_driver.log'
J4 = ['W1', 'W2', 'OOS', 'IS']
H5 = ['HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E']
SPLIT_A = 'W2,OOS,HOLD-A,HOLD-B,IS-1'
SPLIT_B = 'W1,HOLD-C,HOLD-D,HOLD-E,IS-2'
# 段定义:(stage, 轴)。基座由上一段选出;K1 的基座是"pv_mult=5"(用户令的起点)
STAGES = [('K2', {'pv_thr': [-0.005, -0.02]}),
          ('K3', {'trailing_floor': [0.01, 0.04], 'trailing_k': [0.15]}),
          ('K4', {'funding_stop': [0.003, 1.0]})]


def log(s):
    line = '[%s] %s' % (time.strftime('%H:%M:%S'), s)
    open(LOG, 'a').write(line + '\n')
    print(line, flush=True)


def parse(path, stage):
    """→ {(win, arm): dict}。IS-1/IS-2 保留原样,由 agg() 合并。"""
    out, raw = {}, 0
    if not os.path.exists(path):
        return out
    for ln in open(path):
        if not ln.startswith(stage + '/'):
            continue
        raw += 1
        h, r = ln.split(':', 1)
        w = h.split('/')[1]
        arm = r.split()[0]

        def g(k, p=r'\s*(-?[\d.eE+]+)'):
            m = re.search(k + p, ln)
            return float(m.group(1)) if m else np.nan
        f = g('fills', r'\s*([\d.]+)')
        out[(w, arm)] = dict(ret=DC.corrected_ret(g('ret'), f), mdd=abs(g('mdd')),
                             fills=f, grids=g('格'))
    assert len(out) == raw, '%s 解析 %d != 行 %d(静默丢样本)' % (path, len(out), raw)
    return out


def agg(res, arms):
    """IS-1/IS-2 的 ret 用两段和近似整窗(段内 days=61,合计≈整窗量级);MDD 取两段最大(保守)。"""
    rows = []
    for a in arms:
        r = {}
        ok = True
        for w in J4 + H5:
            if w == 'IS':
                k1, k2 = res.get(('IS-1', a)), res.get(('IS-2', a))
                if not k1 or not k2:
                    ok = False; break
                r[w] = (k1['ret'] + k2['ret'], max(k1['mdd'], k2['mdd']))
            else:
                k = res.get((w, a))
                if not k:
                    ok = False; break
                r[w] = (k['ret'], k['mdd'])
        if ok:
            rows.append(dict(arm=a, **{w: r[w] for w in J4 + H5}))
    return rows


def run_stage(stage, base, axis, budget):
    env = dict(os.environ, K_STAGE=stage, K_BASE=json.dumps(base),
               K_AXIS=json.dumps(axis), K_BUDGET=str(budget), BT_WORKERS='3',
               OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    procs = []
    for tag, wins in (('a', SPLIT_A), ('b', SPLIT_B)):
        e = dict(env, K_WINS=wins)
        f = open('%s/%s_%s.log' % (A, stage.lower(), tag), 'a')
        procs.append(subprocess.Popen(
            [PY, '-u', RD + '/eff1_chain_scan.py'], env=e, stdout=f, stderr=f))
        time.sleep(25)                     # 错开,避免两个 tick 表构建撞一起
    log('%s 两作业已起 pid=%s' % (stage, [p.pid for p in procs]))
    for p in procs:
        p.wait()
    log('%s 两作业退出 码=%s' % (stage, [p.returncode for p in procs]))


def pick(stage, base_arm='BASE'):
    """按写死规则选点。返回 (最优臂名, 该臂 chain_ov 增量) 或 None(该轴不动)。"""
    res = parse('%s/eff1_%s_results.txt' % (A, stage.lower()), stage)
    arms = sorted({a for _w, a in res})
    rows = agg(res, arms)
    if not rows:
        log('%s 无完整臂,该轴不动' % stage); return None
    prod = parse(A + '/eff1_scan_v2_results.txt', 'P1')
    pmdd = {w: prod.get((w, 'geo_b3_c16'), {}).get('mdd', np.nan) for w in J4 + H5}
    if np.isnan(pmdd.get('IS', np.nan)):
        pmdd['IS'] = prod.get(('IS', 'geo_b3_c16'), {}).get('mdd', np.nan)
    log('%s 结果(%d 臂):' % (stage, len(rows)))
    best, bestv = None, -1e18
    for r in rows:
        n9 = sum(r[w][0] for w in J4 + H5)
        n4 = sum(r[w][0] for w in J4)
        n5 = sum(r[w][0] for w in H5)
        over = [w for w in J4 + H5 if r[w][1] > pmdd[w] * 1.3]
        log('   %-12s 九窗%+8.2f  判定4%+8.2f  留出5%+8.2f  MDD越限%s'
            % (r['arm'], n9, n4, n5, over if over else '无'))
        if not over and n9 > bestv:
            best, bestv = r['arm'], n9
    if best is None:
        log('%s 全部越 MDD 上限 ⇒ 该轴不动' % stage); return None
    b0 = next((sum(r[w][0] for w in J4 + H5) for r in rows if r['arm'] == base_arm), None)
    if b0 is not None and bestv <= b0:
        log('%s 最优(%s %+.2f)未超基座(%+.2f)⇒ 该轴不动' % (stage, best, bestv, b0))
        return None
    log('%s ⇒ 选中 **%s**(九窗 %+.2f)' % (stage, best, bestv))
    return best


def main():
    log('=' * 70)
    log('贪心驱动器启动。选点规则:主键=九窗合计ret(密度修正);硬约束=无窗 MDD>现值×1.3;')
    log('  轴无点满足或不优于基座 ⇒ 该轴不动。**不碰 HOLD-F/JUL26**。')
    # K1 由外部已启动,此处等它跑完
    while subprocess.run(['pgrep', '-f', 'eff1_k1_pv5stop'],
                         capture_output=True).returncode == 0:
        time.sleep(60)
    log('K1 已结束,开始选点')
    res = parse(A + '/eff1_k1_results.txt', 'K1')
    arms = sorted({a for _w, a in res})
    rows = agg(res, arms)
    prod = parse(A + '/eff1_scan_v2_results.txt', 'P1')
    pmdd = {w: prod.get((w, 'geo_b3_c16'), {}).get('mdd', np.nan) for w in J4 + H5}
    best, bestv = None, -1e18
    log('K1 结果(%d 臂):' % len(rows))
    for r in rows:
        n9 = sum(r[w][0] for w in J4 + H5); n4 = sum(r[w][0] for w in J4)
        n5 = sum(r[w][0] for w in H5)
        over = [w for w in J4 + H5 if r[w][1] > pmdd[w] * 1.3]
        log('   %-12s 九窗%+8.2f  判定4%+8.2f  留出5%+8.2f  MDD越限%s'
            % (r['arm'], n9, n4, n5, over if over else '无'))
        if not over and n9 > bestv:
            best, bestv = r['arm'], n9
    if best is None:
        log('K1 全部越 MDD 上限 ⇒ 退回生产现值,流水线终止'); return
    stop = float(re.match(r's([\d.]+)_m5', best).group(1)) / 100.0
    base = {'pv_mult': 5, 'stop_loss': stop}
    log('K1 ⇒ 选中 **%s**(九窗 %+.2f)⇒ 基座 = %s' % (best, bestv, base))
    budget = 18
    for stage, axis in STAGES:
        budget += sum(len(v) for v in axis.values())
        log('--- %s 开跑,基座=%s 轴=%s 累计预算=%d ---' % (stage, base, axis, budget))
        run_stage(stage, base, axis, budget)
        w = pick(stage)
        if w:
            for k, vals in axis.items():
                for v in vals:
                    nm = '%s%s' % (k.replace('_', '')[:6],
                                   ('%g' % (v * 1000)) if abs(v) < 0.1 else ('%g' % v))
                    if nm == w:
                        base = dict(base); base[k] = v
            log('%s ⇒ 基座更新为 %s' % (stage, base))
    log('=' * 70)
    log('贪心搜索完成。最终基座 = %s  累计搜索预算 = %d 点' % (base, budget))
    log('**下一步需人工**:写死预注册(冻结候选/判据/失败含义)→ 经审阅 → 才可碰 HOLD-F/JUL26')
    open(A + '/greedy_final_base.json', 'w').write(json.dumps(base, indent=2))


if __name__ == '__main__':
    main()
