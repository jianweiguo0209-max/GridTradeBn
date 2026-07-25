"""IS 窗分段跑(2026-07-26 用户令"IS拆成两段跑"+"分段可以并行")。

**口径不变是前提**:组合级 Calmar 由整窗净值曲线定义(12 offset lane 各自按平仓时刻
复利 → 等权平均 → 年化/MDD)。两段各算 Calmar 无法合并,且其余 18 臂的 IS 是**整窗**跑
(EP2_s030=20.3),口径不一致即不可比。
故本脚本:两段各自 preload+run_arm 只落**逐格明细**,merge 阶段合并明细后按**整窗 days**
调 SW.metrics ⇒ 与整窗跑逐位一致(metrics 只依赖逐格 run_time/offset/pnl_ratio)。
内存收益:每段 load_full_series(1m) 工作集减半,两段可并行而不撞 OOM。

用法:
  rsp2_is_split.py seg 1      # 跑 IS-a(03-01~04-30),落 rsp2_is_seg1.parquet
  rsp2_is_split.py seg 2      # 跑 IS-b(05-01~06-30),可与 seg1 并行
  rsp2_is_split.py merge      # 合并两段 → 自检 → emit 正式结果
merge 阶段先用 EP2_s030 对整窗存档自检(C20.3/ret+26.93/MDD5.13),
**不复现即停手、不写任何正式结果**。
"""
import importlib.util
import os
import sys
import time
from collections import Counter

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
OUT = RD + '/ablation'
_spec = importlib.util.spec_from_file_location('r2', RD + '/rsp2_final_bt.py')
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

SEGS = {1: ('2026-03-01', '2026-04-30'), 2: ('2026-05-01', '2026-06-30')}
FULL = ('2026-03-01', '2026-06-30')
SELF_CHECK = ('EP2_s030', 20.3, 26.93, 5.13)      # 整窗存档 Calmar/ret%/MDD%
# 自检臂 + 待跑新臂(eff1×6 + EPE×2)
ARMS = ([a for a in R.ARMS if a[0] == SELF_CHECK[0]]
        + [a for a in R.ARMS if a[1] in ('eff1', 'EPE')])


def seg_path(i):
    return '%s/rsp2_is_seg%d.parquet' % (OUT, i)


def run_seg(i):
    s0, e0 = SEGS[i]
    cache = ParquetCache(V.default_cache_root())
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    SW.set_baseline({})
    pool = pd.read_parquet(R.pool_path('IS'))
    ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
    out = []
    for ranker in ('EP2', 'eff1', 'EPE'):          # 每选币器 preload 一次,其链共享
        arms_r = [a for a in ARMS if a[1] == ranker]
        if not arms_r:
            continue
        t0 = time.time()
        picks = R.make_picks(pool, ranker, 'IS')
        picks = [p for p in picks if ws <= p[0] < we]          # 按段切轮
        wd = R.preload_from_picks(cache, picks, 'IS', s0, e0, universe)
        print('[seg%d] %s preload %.1fmin 格=%d 币=%d'
              % (i, ranker, (time.time() - t0) / 60, len(wd.raw), wd.n_symbols), flush=True)
        pv_cache = {}
        for name, _rk, ov in arms_r:
            t0 = time.time()
            df = SW.run_arm(wd, SW.Arm('rsp2', name, ov), pv_cache, workers=2)
            df['_arm'] = name
            out.append(df)
            print('[seg%d] %-12s 格=%d %.1fmin' % (i, name, len(df),
                                                   (time.time() - t0) / 60), flush=True)
        del wd
    pd.concat(out, ignore_index=True).to_parquet(seg_path(i))
    print('[seg%d] DONE 落盘 %s' % (i, seg_path(i)), flush=True)


def merge():
    for i in SEGS:
        if not os.path.exists(seg_path(i)):
            print('缺 %s,先跑 seg %d' % (seg_path(i), i))
            return
    d = pd.concat([pd.read_parquet(seg_path(i)) for i in SEGS], ignore_index=True)
    days = int((pd.Timestamp(FULL[1]) - pd.Timestamp(FULL[0])).days) + 1
    nm, c0, r0, m0 = SELF_CHECK
    sc = d[d['_arm'] == nm]
    m = SW.metrics(sc.drop(columns=['_arm']), days)
    ok = (abs(m['calmar'] - c0) <= 0.15 and abs(m['ret'] * 100 - r0) <= 0.02
          and abs(m['mdd'] * 100 - m0) <= 0.02)
    print('[自检] %s 分段合并 C%.1f/ret%+.2f/MDD%.2f vs 整窗存档 C%.1f/ret%+.2f/MDD%.2f → %s'
          % (nm, m['calmar'], m['ret'] * 100, m['mdd'] * 100, c0, r0, m0,
             'PASS 口径等价' if ok else '**FAIL 停手**'), flush=True)
    if not ok:
        return
    for name, _rk, _ov in ARMS:
        if name == nm:
            continue
        g = d[d['_arm'] == name]
        if g.empty:
            continue
        mm = SW.metrics(g.drop(columns=['_arm']), days)
        er = Counter(g['exit_reason'])
        top = ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(4))
        R.emit('MAIN/IS: %-12s ret%+7.2f mdd%6.2f calmar%7.1f 格%d 破%d 爆%d 固%d pv%d '
               '最差%+.3f 0.0min | %s'
               % (name, mm['ret'] * 100, -mm['mdd'] * 100, mm['calmar'], mm['n_grids'],
                  mm['n_broke'], mm['n_blown'], mm['n_fixstop'], mm['n_pvstop'],
                  mm['worst_grid'], top))
    print('RSP2_IS_MERGE_DONE', flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'seg':
        run_seg(int(sys.argv[2]))
    else:
        merge()
