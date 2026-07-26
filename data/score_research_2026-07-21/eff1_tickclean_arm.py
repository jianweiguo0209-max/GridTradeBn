"""新臂:eff1 × **tick-clean 票池** × b3_c16 × s030,九窗(2026-07-26,用户令)。

**为什么是新臂而不是修正**:在选币阶段剔掉"tick 粗到装不下网格"的币、让次排位递补,
在 OOS 窗会有 **73~84% 的轮换币**(平均用到 5 个候选里的第 2.67~3.11 名)。
那已经不是补位,是换了一套选币逻辑 ⇒ 得到的是**新臂**,必须走完整判定/留出双验,
**不得用它来重解释旧读数**。

**过滤规则**:候选按 rank 升序,逐个算 `calc_grid_params_v2` 的 spacing,
若 `spacing < min_ticks × tick(该币该日)` 则剔除;剩下的交给 `allocate_with_tiers`
按原有 `pick_first_allowed` 递补(机制现成,不需改)。缺 tick 的币 **fail-open 保留**
(与 lot_by_sym 缺表同款语义)。整轮候选全被剔 ⇒ 该轮空过(OOS 实测 ~3%)。

**阈值依据(实测非拍脑袋)**:off/stack 两种建模的成交数分叉曲线(tick_divergence):
  <1tick 44.6× | 1~1.5 26.0× | 1.5~2 19.6× | **2~3 3.9×** | 3~5 1.44× | 5~10 1.25×
  | 10~20 1.12× | 20~50 1.04× | >50 1.004×
  实盘间距/tick 最小 23、中位 133 ⇒ 落在 1.04×/1.004× 的收敛区,这正是实盘标定
  fill_rate=1.004 成立的原因。取 min_ticks=3(分叉降到 3.9× 以下)。

**几何为何固定 b3_c16**:第一段九窗扫描已定案——剔除 tick 污染窗(OOS/IS)后,
候选 b2.5_c16 对现值 b3_c16 在干净六窗是 **3:3 平手**(合计 +2.59 vs +0.05),
其全部优势(72.75pp)集中在被剔的两窗 ⇒ 几何轴保现值。

**字节复现门**:`MIN_TICKS=0` 时过滤关闭,九窗必须逐位回到扫描存档 P1/geo_b3_c16。

用法: [MIN_TICKS=3] [BT_WINDOWS=W1,OOS] eff1_tickclean_arm.py
"""
import importlib.util
import os
import sys
import time

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
os.environ.setdefault('WN', 'OOS')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.core.grid_params import calc_grid_params_v2

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_s = importlib.util.spec_from_file_location('sc', RD + '/eff1_scan.py')
S = importlib.util.module_from_spec(_s)
_s.loader.exec_module(S)
_t = importlib.util.spec_from_file_location('tt', RD + '/b2c26_trim_top.py')
T = importlib.util.module_from_spec(_t)
_t.loader.exec_module(T)
_g = importlib.util.spec_from_file_location('tg', RD + '/tick_gate_ninewin.py')
G = importlib.util.module_from_spec(_g)
_g.loader.exec_module(G)

MIN_TICKS = float(os.environ.get('MIN_TICKS', '3'))
WORKERS = int(os.environ.get('WORKERS', '2'))
SEG = os.environ.get('SEG', '')            # IS 分段:1|2|merge(122天/2735格,整窗预热吃紧)
IS_SEGS = {'1': ('2026-03-01', '2026-04-30'), '2': ('2026-05-01', '2026-06-30')}


def seg_path(i):
    return RD + '/ablation/tickclean_IS_seg%s_k%g.parquet' % (i, MIN_TICKS)


def merge_is():
    import numpy as np
    parts = []
    for i in IS_SEGS:
        if not os.path.exists(seg_path(i)):
            print('缺 %s,先跑 SEG=%s' % (seg_path(i), i)); return
        parts.append(pd.read_parquet(seg_path(i)))
    d = pd.concat(parts, ignore_index=True)
    s0, e0 = S.WD9['IS']
    days = int((pd.Timestamp(e0) - pd.Timestamp(s0)).days) + 1
    m = SW.metrics(d, days)
    r0, m0 = REF['IS']
    gate = ''
    if MIN_TICKS == 0:
        ok = abs(m['ret'] * 100 - r0) < 0.005 and abs(m['mdd'] * 100 - m0) < 0.005
        gate = '  [字节门 %s]' % ('PASS' if ok else '**FAIL**')
    line = ('IS(merge) ret%+8.2f mdd%6.2f calmar%9.3f fills%6.1f 格%-5d  (锚 ret%+.2f/mdd%.2f)%s'
            % (m['ret'] * 100, -m['mdd'] * 100, m['calmar'], m['n_fills'], len(d), r0, m0, gate))
    print(line, flush=True)
    with open(OUT, 'a') as f:
        f.write(line + '\n')
GEO = {'band': 3, 'count_min': 16}                     # 现值几何
OUT = RD + '/ablation/eff1_tickclean_results.txt'
# 扫描存档 P1/geo_b3_c16(字节复现门的锚)
REF = {'W1': (3.77, 3.26), 'W2': (6.81, 3.07), 'OOS': (20.79, 2.76), 'IS': (24.26, 3.19),
       'HOLD-A': (-2.71, 5.69), 'HOLD-B': (5.33, 2.86), 'HOLD-C': (1.99, 3.58),
       'HOLD-D': (-1.99, 5.31), 'HOLD-E': (2.99, 4.38)}


def tick_filter(picks, tk, min_ticks):
    """剔除 spacing < min_ticks×tick 的候选。缺 tick → fail-open 保留。"""
    if min_ticks <= 0:
        return picks, 0
    v2 = dict(SW._V2, atr_range_multiplier=GEO['band'], grid_count_min=GEO['count_min'],
              grid_spacing_max=SW.baseline()['spacing_max'])
    out, drop = [], 0
    for rt, off, row in picks:
        m = tk.get(row['symbol'])
        t = m.get(pd.Timestamp(rt).date()) if m else None
        if not t or t != t:
            out.append((rt, off, row))
            continue
        try:
            p = calc_grid_params_v2(row=row, price_limit=SW._S['price_limit'],
                                    stop_limit=SW._S['stop_limit'], v2_config=v2)
        except Exception:
            out.append((rt, off, row))
            continue
        if (p['high_price'] - p['low_price']) / p['grid_count'] >= min_ticks * t:
            out.append((rt, off, row))
        else:
            drop += 1
    return out, drop


def main():
    SW.set_baseline({})
    if SEG == 'merge':
        return merge_is()
    only = [w for w in os.environ.get('BT_WINDOWS', '').split(',') if w]
    wins = {w: v for w, v in S.WD9.items() if not only or w in only}
    if SEG in IS_SEGS:                  # 只跑 IS 的一段
        wins = {'IS': IS_SEGS[SEG]}
    cache = ParquetCache(V.default_cache_root())
    fh = open(OUT, 'a')

    def emit(s):
        print(s, flush=True)
        fh.write(s + '\n')
        fh.flush()
    emit('== eff1_tickclean MIN_TICKS=%g GEO=b%g_c%d 开跑 %s =='
         % (MIN_TICKS, GEO['band'], GEO['count_min'], time.strftime('%m-%d %H:%M')))
    for wn, (s0, e0) in wins.items():
        t0 = time.time()
        T.WN = wn                       # build_wd 的 WindowData 标签跟窗走
        pool = pd.read_parquet(S.pool_path(wn))
        picks = S.make_picks(pool, 'K1', wn)
        tk = {}
        if MIN_TICKS > 0:
            lo = str((pd.Timestamp(s0) - pd.Timedelta(days=4)).date())
            hi = str((pd.Timestamp(e0) + pd.Timedelta(days=1)).date())
            for sym in sorted({r['symbol'] for _, _, r in picks}):
                tk[sym] = G.daily_tick(cache, sym, lo, hi)
        kept, drop = tick_filter(picks, tk, MIN_TICKS)
        # 复用 T.build_wd 的其余步骤:注入过滤后的 picks
        wd = T.build_wd(cache, s0, e0, picks_override=kept)
        df = SW.run_arm(wd, SW.Arm('eff1', 'tickclean', dict(GEO)), {}, workers=WORKERS)
        if SEG in IS_SEGS:              # 分段只落逐格明细,指标留给 merge 按整窗 days 算
            df.to_parquet(seg_path(SEG))
            emit('IS-seg%s 格=%d 剔候选=%d %.1fmin → %s(段内指标仅监看,正式读数以 merge 为准)'
                 % (SEG, len(df), drop, (time.time() - t0) / 60,
                    os.path.basename(seg_path(SEG))))
            continue
        m = SW.metrics(df, wd.days)
        r0, m0 = REF[wn]
        gate = ''
        if MIN_TICKS == 0:
            ok = abs(m['ret'] * 100 - r0) < 0.005 and abs(m['mdd'] * 100 - m0) < 0.005
            gate = '  [字节门 %s]' % ('PASS' if ok else '**FAIL**')
        emit('%-8s ret%+8.2f mdd%6.2f calmar%9.3f fills%6.1f 格%-5d 剔候选%-5d %.1fmin'
             '  (锚 ret%+.2f/mdd%.2f)%s'
             % (wn, m['ret'] * 100, -m['mdd'] * 100, m['calmar'], m['n_fills'],
                len(df), drop, (time.time() - t0) / 60, r0, m0, gate))
        del wd
    fh.close()


if __name__ == '__main__':
    main()
