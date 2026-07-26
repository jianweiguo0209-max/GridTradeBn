"""K1:在 pv_mult=5 基础上扫固定止损 5 档 × 九窗(b3_c16 几何固定)。

**性质:知识扫描,不是选点。** 九窗(含留出 HOLD-A~E)在本 session 已全部消费,
本层产出**没有干净的地方可以验证**;唯一处女地是 HOLD-F/JUL26,且只能花一次。
⇒ 本层结果**只产参数响应地图**;若要把某点变成可部署结论,必须另写预注册、
   冻结候选、在 HOLD-F/JUL26 上看一次。**不得用本层数据直接下部署结论。**

**为什么值得跑(机制假设)**:`stop2.5` 与 `pvmult5` 是两个**独立的单参数偏移**
(各自从生产基线出发,从未叠加)。二者都是"让格更早/更晚退出"的旋钮,作用域重叠,
故叠加效果不可从单轴读数外推(memory: 单族冠军不可叠加)。三种可能:
  ① 叠加增益——作用在不同退出路径(固损 vs pv),不冲突
  ② 相互抵消——pv_mult=5 少触发 pv ⇒ 更多格活到固损那关;固损收紧又砍回来(机制上最可疑)
  ③ 一方失效——pv_mult=5 已把该止的都止了,固损从 3.0 收到 2.5 无事可做
本扫描直接分辨这三种。

**臂**:`pv_mult=5` × `stop_loss ∈ {1.0, 1.5, 2.0, 2.5, 3.0}%`,其余链参数 = 生产基线。
**内建字节门**:`s3.0_m5` ≡ 现有 `P5/pvmult5`(只改 pv_mult 的臂,stop 本就是基线 3.0),
须逐位复现 T1 重扫的 9 窗读数。不符即预热/臂构造漂移。

用法: [BT_WORKERS=4] eff1_k1_pv5stop.py    (断点续跑)
"""
import importlib.util
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

RESULTS = RD + '/ablation/eff1_k1_results.txt'
GEO = {'band': 3, 'count_min': 16}                 # 生产现值几何,冻结
STOPS = (1.0, 1.5, 2.0, 2.5, 3.0)
ARMS = [('s%.1f_m5' % s, {'stop_loss': s / 100.0, 'pv_mult': 5}) for s in STOPS]
ALL_WINS = ['W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E']
# IS 分两段(122天/2735格):既降单进程峰值,又把工作量切成更均匀的单元供多窗并行。
# ⚠ 分段与整窗差 1 格(allocate_with_tiers 的 held 状态在接缝重置,实测 ret 差 0.04pp)
#   ⇒ IS 的字节门降级为容差 0.05pp;其余八窗仍是逐位门。
IS_SEGS = {'IS-1': ('2026-03-01', '2026-04-30'), 'IS-2': ('2026-05-01', '2026-06-30')}
WINDOW_RANGE = dict(IS_SEGS)
_sel = [w for w in os.environ.get('K1_WINS', '').split(',') if w]
WINS = _sel if _sel else [w for w in ALL_WINS if w != 'IS'] + list(IS_SEGS)
assert not ({'HOLD-F', 'JUL26'} & set(WINS)), '处女终审窗禁入'


def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


def done_set():
    d = set()
    try:
        for ln in open(RESULTS):
            if ln.startswith('K1/'):
                p = ln.split(':', 1)
                if len(p) == 2 and p[1].split():
                    d.add((p[0].split('/')[1], p[1].split()[0]))
    except FileNotFoundError:
        pass
    return d


def main():
    cache = ParquetCache(V.default_cache_root())
    w_sim = int(os.environ.get('BT_WORKERS', '4'))
    universe = sorted(set(V.list_archive_symbols())
                      - set(effective_blacklist((), DEFAULT_TIER_POLICY)))
    SW.set_baseline({})
    emit('== K1 pv_mult=5 × 固损5档 × 九窗 (geo b3_c16 冻结) 开跑 %s =='
         % time.strftime('%m-%d %H:%M'))
    emit('== 性质:知识扫描,不选点;结果不得直接下部署结论 ==')
    done = done_set()
    for wn in WINS:
        s0, e0 = WINDOW_RANGE.get(wn) or S.WD9[wn]
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
        base = S.make_picks(pool, 'K1', pool_wn)
        tk = {sym: G.daily_tick(cache, sym, lo, hi)
              for sym in sorted({r['symbol'] for _, _, r in base})}
        series = {}
        raw_picks = S.make_picks(pool, 'K1', pool_wn)
        if wn.startswith('IS-'):      # 按段切轮(整窗时不做,见 build_wd 的同款教训)
            _ws, _we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
            raw_picks = [p for p in raw_picks if _ws <= p[0] < _we]
        picks, drop = V2.tick_filter(raw_picks, tk, GEO)
        wd = V2.preload(cache, picks, wn, s0, e0, blocked, series)
        emit('[%s] 预热 %.1fmin 格=%d 剔候选=%d(几何固定,5臂共享)'
             % (wn, (time.time() - t0) / 60, len(wd.raw), drop))
        pv_cache = {}
        for name, chain_ov in todo:
            t0 = time.time()
            ov = dict(chain_ov); ov.update(GEO)
            df = SW.run_arm(wd, SW.Arm('eff1', name, ov), pv_cache, workers=w_sim)
            m = SW.metrics(df, wd.days)
            er = Counter(df['exit_reason'])
            gate = '  [≡P5/pvmult5,须逐位对上]' if name == 's3.0_m5' else ''
            emit('K1/%s: %-10s ret%+9.2f mdd%6.2f calmar%10.4g fills%7.1f 格%d 破%d %.1fmin | %s%s'
                 % (wn, name, m['ret'] * 100, -m['mdd'] * 100, m['calmar'],
                    float(df['n_fills'].mean()), m['n_grids'], m['n_broke'],
                    (time.time() - t0) / 60,
                    ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(3)), gate))
        del wd, series
        emit('[%s] DONE' % wn)
    emit('K1_DONE')


if __name__ == '__main__':
    main()
