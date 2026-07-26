"""链轴束搜索 v2 驱动器(S0→S1→S2)—— 严格执行预注册 2026-07-27-chain-beam-prereg。

**规则(§2,出数前已冻结,此处只执行,不判断)**:
  ① 现值地板:九窗合计 ret(密度修正后)> 生产现值
  ② Calmar 约束:九窗合计**对数 Calmar** ≥ 生产现值同口径
     (对数年化 (365/d)·ln(1+ret)/MDD;与标准年化在当前量级排序相同,
      但不带 6.19 次幂的定时炸弹,见 calmar-primary-metric-power-law-flaw)
  ③ 灾难上限:**无任何窗** MDD > 生产现值同窗 × 2.0
  ④ 满足 ①~③ 者按九窗合计 ret 降序取前 3 入束
  ⑤ 某段无点满足 ⇒ 束不变  ⑥ 全程无点 ⇒ 终止保现值,不进裁决

**S0 已完成**(纯读 T1 已有数据):束 = [pvmult5] = {pv_mult:5}。
**S1/S2**:束内每点沿每轴取全部值,去重、去已跑(K1 已跑 pv_mult=5×stop{1.0~3.0})。

**不碰 HOLD-F/JUL26**(硬断言)。束落盘后**停,等人工审阅**再裁决。
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

LOG = A + '/beam_v2.log'
REG = A + '/beam_v2_registry.json'          # 臂名 → 配置
# ⚠ 束宽 3→4 是**对冻结预注册的偏离**(用户令 2026-07-27 07:2x,写于 S2 出任何新读数之前,
#   但在 S1 全部读数已知之后)。加入的是 S1 第 4 名 s2.5_m5+pvthr-5(九窗 +37.79、
#   留出5 +11.44 全场第二)。代价:搜索预算 53→60 点、终局候选可能 4 个
#   ⇒ 裁决 §3(ii) 的 Bonferroni 门槛须从 t≥2.5 提到 **t≥2.6**(精确 2.531)。
#   偏离已记入 docs/superpowers/specs/2026-07-27-chain-beam-prereg.md §7。
BEAM = 4
J4 = ['W1', 'W2', 'OOS', 'IS']
H5 = ['HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E']
DAYS = {'W1': 61, 'W2': 61, 'OOS': 59, 'IS': 122,
        'HOLD-A': 59, 'HOLD-B': 61, 'HOLD-C': 61, 'HOLD-D': 62, 'HOLD-E': 75}
SPLIT_A = 'W2,OOS,HOLD-A,HOLD-B,IS-1'
SPLIT_B = 'W1,HOLD-C,HOLD-D,HOLD-E,IS-2'
AXES = {'stop_loss': [0.010, 0.015, 0.020, 0.025, 0.030],
        'pv_thr': [-0.005, -0.010, -0.020],
        'pv_mult': [3, 5],
        'trailing_floor': [0.01, 0.02, 0.04],
        'trailing_k': [0.15, 0.30],
        'funding_stop': [0.0015, 0.003, 1.0]}
PROD_CFG = {'stop_loss': 0.030, 'pv_thr': -0.010, 'pv_mult': 3,
            'trailing_floor': 0.02, 'trailing_k': 0.30, 'funding_stop': 0.0015}
assert not ({'HOLD-F', 'JUL26'} & (set(SPLIT_A.split(',')) | set(SPLIT_B.split(',')))), \
    '处女终审窗禁入'


def log(s):
    line = '[%s] %s' % (time.strftime('%H:%M:%S'), s)
    open(LOG, 'a').write(line + '\n')
    print(line, flush=True)


def _load(path, pref, keep=None):
    o = {}
    if not os.path.exists(path):
        return o
    raw = 0
    for ln in open(path):
        if not ln.startswith(pref):
            continue
        raw += 1
        h, r = ln.split(':', 1)
        w, arm = h.split('/')[1], r.split()[0]
        if keep and arm not in keep:
            continue

        def g(k, p=r'\s*(-?[\d.eE+]+)'):
            m = re.search(k + p, ln)
            return float(m.group(1)) if m else np.nan
        f = g('fills', r'\s*([\d.]+)')
        o[(w, arm)] = dict(ret=DC.corrected_ret(g('ret'), f), mdd=abs(g('mdd')))
    return o


def all_results():
    """汇总 T1(P1现值/P3/P5)+ K1 + S1/S2 的全部读数。"""
    res = {}
    res.update(_load(A + '/eff1_scan_v2_results.txt', 'P1/', keep={'geo_b3_c16'}))
    for p in ('P3/', 'P5/'):
        res.update(_load(A + '/eff1_scan_v2_results.txt', p))
    res.update(_load(A + '/eff1_k1_results.txt', 'K1/'))
    for st in ('S1', 'S2'):
        res.update(_load('%s/eff1_%s_results.txt' % (A, st.lower()), st + '/'))
    return res


def merged(res, arm):
    """IS 由 IS-1+IS-2 合并(ret 相加、MDD 取大);缺任一窗 → None。"""
    r = {}
    for w in J4 + H5:
        if w == 'IS' and (w, arm) not in res:
            a, b = res.get(('IS-1', arm)), res.get(('IS-2', arm))
            if not a or not b:
                return None
            r[w] = (a['ret'] + b['ret'], max(a['mdd'], b['mdd']))
        else:
            k = res.get((w, arm))
            if not k:
                return None
            r[w] = (k['ret'], k['mdd'])
    return r


def logcal(r):
    return sum((365.0 / DAYS[w]) * np.log1p(max(r[w][0] / 100, -0.999)) / (r[w][1] / 100)
               for w in J4 + H5)


def registry():
    if os.path.exists(REG):
        return json.load(open(REG))
    # 初始:T1 单轴 + K1
    reg = {'geo_b3_c16': dict(PROD_CFG)}
    for s in (1.0, 1.5, 2.0, 2.5, 3.0):
        reg['stop%.1f' % s] = dict(PROD_CFG, stop_loss=s / 100.0)
        reg['s%.1f_m5' % s] = dict(PROD_CFG, stop_loss=s / 100.0, pv_mult=5)
    reg['pvthr-0.5'] = dict(PROD_CFG, pv_thr=-0.005)
    reg['pvthr-2'] = dict(PROD_CFG, pv_thr=-0.02)
    reg['pvmult5'] = dict(PROD_CFG, pv_mult=5)
    reg['trF1'] = dict(PROD_CFG, trailing_floor=0.01)
    reg['trF4'] = dict(PROD_CFG, trailing_floor=0.04)
    reg['trK0.15'] = dict(PROD_CFG, trailing_k=0.15)
    reg['fund0.3'] = dict(PROD_CFG, funding_stop=0.003)
    reg['fundOFF'] = dict(PROD_CFG, funding_stop=1.0)
    return reg


def select(res, reg, tag):
    prod = merged(res, 'geo_b3_c16')
    p_ret, p_cal = sum(v[0] for v in prod.values()), logcal(prod)
    log('%s 判据:ret > %+.2f  ΣlogCal ≥ %.2f  无窗 MDD > 现值×2.0' % (tag, p_ret, p_cal))
    rows = []
    for arm in sorted({a for _w, a in res}):
        if arm == 'geo_b3_c16':
            continue
        r = merged(res, arm)
        if not r:
            continue
        ret = sum(v[0] for v in r.values())
        cal = logcal(r)
        over = [w for w in J4 + H5 if r[w][1] > prod[w][1] * 2.0]
        ok = ret > p_ret and cal >= p_cal and not over
        rows.append((ok, ret, arm, sum(r[w][0] for w in J4), sum(r[w][0] for w in H5),
                     cal, over))
    rows.sort(key=lambda x: -x[1])
    for ok, ret, arm, r4, r5, cal, over in rows[:14]:
        log('   %-14s 九窗%+8.2f 判定4%+8.2f 留出5%+8.2f ΣlogCal%7.2f  %s'
            % (arm, ret, r4, r5, cal,
               '✓' if ok else ('✗MDD%s' % over if over else
                               ('✗ret' if ret <= p_ret else '✗Cal'))))
    # ⚠ 按**配置**去重,不是按臂名:同一配置可能有两个臂名(实例:pvmult5 与 s3.0_m5
    #   都是 {pv_mult:5, stop_loss:0.03},分别来自 T1 整窗跑与 K1 分段跑,九窗差 0.19pp
    #   ——那 0.19 正是 IS 整窗 vs 分段的伪影)。不去重会让重复配置占掉束位。
    keep, seen = [], set()
    for ok, _r, a, _4, _5, _c, _o in rows:
        if not ok or a not in reg:
            continue
        key = json.dumps(reg[a], sort_keys=True)
        if key in seen:
            log('   (去重)%s 与已入束者配置相同,跳过' % a)
            continue
        seen.add(key)
        keep.append((a, reg[a]))
        if len(keep) >= BEAM:
            break
    log('%s ⇒ 束(%d): %s' % (tag, len(keep), [a for a, _c in keep]))
    return keep


def beam_tags(beam):
    """给束内每个点一个**保证互不相同**的短前缀。

    ⚠ 不能用 `bn[:8]` 硬截断:第二代臂名前 8 字符会相同
      (`pvmult5+fundi3` 与 `pvmult5+pvthr-5` 都截成 `pvmult5+`)⇒ 展开出的臂名相撞
      ⇒ eff1_chain_scan 的 `臂名重复` 断言把两个作业双双秒崩(2026-07-27 实错)。
    取「能区分束内全部成员的最短前缀」。
    """
    n = 1
    names = [b for b, _c in beam]
    while n < 60 and len({b[:n] for b in names}) < len(names):
        n += 1
    assert len({b[:n] for b in names}) == len(names), '束内臂名无法区分:%s' % names
    return {b: b[:n] for b in names}


def stage_done(st):
    """该段是否已跑满:读扫描器抬头的臂数 × 10 单元,与实际读数比。

    ⚠ 没有这个判断,重启后的驱动器会把「后-S1 的束」当成 S0 束,再连做两轮扩展,
      **编辑距离扩到 4**,超出预注册 §1 冻结的 ≤3(2026-07-27 实错,幸被臂名重复挡下)。
    """
    f = '%s/eff1_%s_results.txt' % (A, st.lower())
    if not os.path.exists(f):
        return False
    n, need = 0, None
    for ln in open(f):
        if ln.startswith(st + '/'):
            n += 1
        else:
            m = re.search(r'\((\d+)臂×', ln)
            if m:
                need = int(m.group(1)) * 10
    return need is not None and n >= need


def expand(beam, res, reg):
    """束内每点沿每轴取全部值,去重、去已跑。

    ⚠ 去重必须对「**已有完整读数**的配置」,不能对注册表 —— 注册表在该段开跑**之前**就写好了,
    对它去重会让重启后的驱动器把本段自己的配置当成"已完成",expand 返回空 ⇒ 直接跳过该段
    写出终局文件(2026-07-27 实错:看门狗误杀驱动器后重启,S1 被整段跳过)。
    """
    seen = {json.dumps(reg[a], sort_keys=True)
            for a in {x for _w, x in res} if a in reg and merged(res, a) is not None}
    tag = beam_tags(beam)
    out = []
    for bn, bc in beam:
        for ax, vals in AXES.items():
            for v in vals:
                if bc.get(ax) == v:
                    continue
                c = dict(bc); c[ax] = v
                key = json.dumps(c, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                sfx = ('%g' % (v * 1000)) if abs(v) < 0.1 else ('%g' % v)
                out.append(('%s+%s%s' % (tag[bn], ax.replace('_', '')[:5], sfx), c))
    assert len({n for n, _c in out}) == len(out), '展开后臂名重复'
    return out


def run(stage, configs):
    env = dict(os.environ, K_STAGE=stage, K_BUDGET='v2', BT_WORKERS='3',
               OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
               K_CONFIGS=json.dumps([[n, c] for n, c in configs]))
    ps = []
    for tag, wins in (('a', SPLIT_A), ('b', SPLIT_B)):
        f = open('%s/%s_%s.log' % (A, stage.lower(), tag), 'a')
        ps.append(subprocess.Popen([PY, '-u', RD + '/eff1_chain_scan.py'],
                                   env=dict(env, K_WINS=wins), stdout=f, stderr=f))
        time.sleep(20)
    log('%s 两作业已起 pid=%s(%d 臂 × 10 单元)' % (stage, [p.pid for p in ps], len(configs)))
    for p in ps:
        p.wait()
    log('%s 退出码=%s' % (stage, [p.returncode for p in ps]))


def main():
    log('=' * 74)
    log('束搜索 v2 启动 B=%d。规则见 2026-07-27-chain-beam-prereg §2(已冻结)。' % BEAM)
    reg = registry()
    res = all_results()
    todo = [st for st in ('S1', 'S2') if not stage_done(st)]
    fin = [st for st in ('S1', 'S2') if st not in todo]
    log('已完成的段:%s;待跑:%s' % (fin or ['无'], todo or ['无']))
    beam = select(res, reg, fin[-1] if fin else 'S0')
    if not beam:
        log('无点满足 ⇒ 保持现值,终止'); return
    for stage in todo:
        cfgs = expand(beam, res, reg)
        # ⚠ 基座门必须**按段命名**:两段都叫 BASE_0 会让 S2 的注册覆写 reg['BASE_0'],
        #   于是 S1 的 BASE_0 读数被按 S2 的配置解释;且 res 以 (窗,臂名) 为键,
        #   两段同名臂会互相覆盖。加段前缀彻底隔离。
        gates = [('%sG%d' % (stage, i), dict(c)) for i, (_n, c) in enumerate(beam)]
        allc = gates + cfgs
        for n, c in allc:
            # ⚠ res 以 (窗,臂名) 为键跨段合并;同名不同配置会让旧读数被按新配置解释。
            if n in reg and json.dumps(reg[n], sort_keys=True) != json.dumps(c, sort_keys=True):
                raise SystemExit('臂名 %s 已指向不同配置,拒绝覆写(会污染跨段读数)' % n)
            reg[n] = c
        json.dump(reg, open(REG, 'w'), indent=1, ensure_ascii=False)
        log('--- %s:束 %d 点扩出 %d 新配置(+%d 基座门)= %d 臂 ---'
            % (stage, len(beam), len(cfgs), len(gates), len(allc)))
        # ⚠ cfgs 为空**不等于**该段该跳过 —— 重启时该段可能已跑全,expand 把它们全去重掉了。
        #   此时仍须选点(select 看的是全量读数,与段无关),只是不必再跑。
        #   直接 break 会把整段的选点跳过并写出终局文件(2026-07-27 级联事故的同一个坑)。
        if cfgs:
            run(stage, allc)
            res = all_results()
        else:
            log('%s 无新配置可扩(该段已跑全或已被覆盖)⇒ 跳过跑,直接选点' % stage)
        nb = select(res, reg, stage)
        if nb:
            beam = nb
        else:
            log('%s 无点满足 ⇒ 束不变' % stage)
    log('=' * 74)
    log('束搜索 v2 完成。最终 %d 个候选:' % len(beam))
    for n, c in beam:
        log('   %-14s %s' % (n, json.dumps(c, sort_keys=True)))
    log('**下一步需人工审阅** → 才可碰 HOLD-F/JUL26(唯一处女地,只看一次)')
    json.dump([{'name': n, 'config': c} for n, c in beam],
              open(A + '/beam_v2_final.json', 'w'), indent=2, ensure_ascii=False)


if __name__ == '__main__':
    main()
