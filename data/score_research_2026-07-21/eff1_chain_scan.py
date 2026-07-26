"""链轴贪心坐标上升 · 通用扫描器(K2/K3/K4… 共用)。

**用法**:
    K_STAGE=K2 \
    K_BASE='{"pv_mult":5,"stop_loss":0.025}' \
    K_AXIS='{"pv_thr":[-0.005,-0.02]}' \
    K_WINS=W2,OOS,HOLD-A,HOLD-B,IS-1 \
    BT_WORKERS=3 eff1_chain_scan.py

**性质:知识扫描,不是选点。** 九窗(含留出 HOLD-A~E)已在本 session 全部消费,本层产出
**没有干净的地方可以验证**。唯一处女地 = HOLD-F/JUL26,只能看一次。
⇒ 贪心搜索的终点若要变成可部署结论,**必须先写死预注册**(候选最多 1 个 + 判据 + 失败含义),
再在 HOLD-F/JUL26 上看一次。**不得用本层任何数据直接下部署结论。**

**搜索预算记账**(贪心每段条件于上一段,有效空间大于点数):
    T1 单轴 P3/P5 13 点 + K1 5 点 + K2… 逐段累加。每段开跑前把累计数写进日志抬头。

**几何冻结** b3_c16(几何轴已由 geo-chain-joint 预注册判死并回到保现值)。

**内建字节门**:基座本身(不加任何轴覆写)作为一臂跑,须复现上一段该配置的读数。
IS 因分段(见下)容差 0.05pp,其余窗逐位。

**IS 分两段**:122天/2735格。既降单进程峰值,又把工作量切成更均匀的单元供**多窗并行**
(每单元约 5 分钟是单线程的 blocked_rts+tick 表+预热,并行时正好互补)。
⚠ 分段与整窗差 1 格(allocate_with_tiers 的 held 状态在接缝重置,实测 ret 差 0.04pp)。
"""
import importlib.util
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
os.environ.setdefault('MIN_TICKS', '3')
os.environ.setdefault('EFF1_LAYER', 'P1')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import BT_UNIVERSE_TOP_PCT
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.shock_replay import blocked_rts
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_v = importlib.util.spec_from_file_location('v2', RD + '/eff1_scan_v2.py')
V2 = importlib.util.module_from_spec(_v)
_v.loader.exec_module(V2)
S, G = V2.S, V2.G

STAGE = os.environ.get('K_STAGE', 'K2')
# K_CONFIGS = JSON list of [name, chain_ov]。由驱动器生成(束搜索:每段对束内每个基座
# 展开该轴的全部取值,基座本身也入列作字节门)。旧的 K_BASE/K_AXIS 接口保留作兜底。
CONFIGS = json.loads(os.environ.get('K_CONFIGS', '[]'))
BASE = json.loads(os.environ.get('K_BASE', '{}'))
AXIS = json.loads(os.environ.get('K_AXIS', '{}'))
BUDGET = os.environ.get('K_BUDGET', '?')
RESULTS = '%s/ablation/eff1_%s_results.txt' % (RD, STAGE.lower())
GEO = {'band': 3, 'count_min': 16}                 # 几何冻结
IS_SEGS = {'IS-1': ('2026-03-01', '2026-04-30'), 'IS-2': ('2026-05-01', '2026-06-30')}
ALL = ['W1', 'W2', 'OOS', 'HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E'] + list(IS_SEGS)
_sel = [w for w in os.environ.get('K_WINS', '').split(',') if w]
WINS = _sel if _sel else ALL
assert not ({'HOLD-F', 'JUL26'} & set(WINS)), '处女终审窗禁入'


def _fmt(v):
    return ('%g' % (v * 1000)) if abs(v) < 0.1 else ('%g' % v)


if CONFIGS:
    ARMS = [(n, ov) for n, ov in CONFIGS]
else:                                              # 兜底:单基座 × 单轴
    ARMS = [('BASE', dict(BASE))]
    for k, vals in AXIS.items():
        for v in vals:
            ov = dict(BASE); ov[k] = v
            ARMS.append(('%s%s' % (k.replace('_', '')[:6], _fmt(v)), ov))
assert ARMS, '无臂可跑'
assert len({n for n, _ in ARMS}) == len(ARMS), '臂名重复'



def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


def done_set():
    d = set()
    try:
        for ln in open(RESULTS):
            if ln.startswith(STAGE + '/'):
                p = ln.split(':', 1)
                if len(p) == 2 and p[1].split():
                    d.add((p[0].split('/')[1], p[1].split()[0]))
    except FileNotFoundError:
        pass
    return d


def main():
    cache = ParquetCache(V.default_cache_root())
    w_sim = int(os.environ.get('BT_WORKERS', '3'))
    universe = sorted(set(V.list_archive_symbols())
                      - set(effective_blacklist((), DEFAULT_TIER_POLICY)))
    SW.set_baseline({})
    emit('== %s 累计搜索预算=%s 开跑 %s (%d臂×%d单元) =='
         % (STAGE, BUDGET, time.strftime('%m-%d %H:%M'), len(ARMS), len(WINS)))
    for _n, _o in ARMS:
        emit('   臂 %-14s %s' % (_n, json.dumps(_o, sort_keys=True)))
    emit('== 性质:知识扫描,不选点;裁决前必须先写死预注册 ==')
    done = done_set()
    for wn in WINS:
        s0, e0 = IS_SEGS.get(wn) or S.WD9[wn]
        pool_wn = 'IS' if wn.startswith('IS-') else wn
        todo = [a for a in ARMS if (wn, a[0]) not in done]
        if not todo:
            emit('[%s] SKIP' % wn); continue
        pool = pd.read_parquet(S.pool_path(pool_wn))
        t0 = time.time()
        blocked = blocked_rts(cache, universe, pd.Timestamp(s0),
                              pd.Timestamp(e0) + pd.Timedelta(days=1), '1h', *SW.SHOCK,
                              min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
        lo = str((pd.Timestamp(s0) - pd.Timedelta(days=4)).date())
        hi = str((pd.Timestamp(e0) + pd.Timedelta(days=1)).date())
        raw = S.make_picks(pool, 'K1', pool_wn)
        tk = {sym: G.daily_tick(cache, sym, lo, hi)
              for sym in sorted({r['symbol'] for _, _, r in raw})}
        if wn.startswith('IS-'):                   # 按段切轮
            ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
            raw = [p for p in raw if ws <= p[0] < we]
        series = {}
        picks, drop = V2.tick_filter(raw, tk, GEO)
        wd = V2.preload(cache, picks, wn, s0, e0, blocked, series)
        emit('[%s] 预热 %.1fmin 格=%d 剔候选=%d(几何固定,%d臂共享)'
             % (wn, (time.time() - t0) / 60, len(wd.raw), drop, len(todo)))
        pv_cache = {}
        for name, chain_ov in todo:
            t0 = time.time()
            ov = dict(chain_ov); ov.update(GEO)
            df = SW.run_arm(wd, SW.Arm('eff1', name, ov), pv_cache, workers=w_sim)
            m = SW.metrics(df, wd.days)
            er = Counter(df['exit_reason'])
            emit('%s/%s: %-12s ret%+9.2f mdd%6.2f calmar%10.4g fills%7.1f 格%d 破%d %.1fmin | %s%s'
                 % (STAGE, wn, name, m['ret'] * 100, -m['mdd'] * 100, m['calmar'],
                    float(df['n_fills'].mean()), m['n_grids'], m['n_broke'],
                    (time.time() - t0) / 60,
                    ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(3)),
                    '  [基座字节门]' if name.startswith('BASE') else ''))
        del wd, series
        emit('[%s] DONE' % wn)
    emit('%s_DONE' % STAGE)


if __name__ == '__main__':
    main()
