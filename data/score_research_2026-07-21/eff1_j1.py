"""J1:几何 × pv 联合搜索(54 组合 × 判定四窗)——预注册 2026-07-26-geo-chain-joint。

**搜索空间(预注册 §2 冻结,不得中途扩张)**:
  几何 band{2,2.5,3} × count_min{16,22,26} = 9
  pv   pv_thr{−0.005, −0.01(现值), −0.02} × pv_mult{3(现值), 5} = 6
  其余链参数 = s030 生产现值全套;票池 = tick-clean(min_ticks=3)
**只跑判定四窗 W1/W2/OOS/IS**。HOLD-A~E 是留出一级、HOLD-F/JUL26 是处女终审,
本层**禁止**在其上运行(预注册 §5 / §6)。

**内建字节门(免费)**:9 个 `pv_thr=−0.01, pv_mult=3` 的组合 = 生产现值 pv,
必须逐位复现 T1 重扫(eff1_scan_v2_results.txt)的 P1/geo_* 读数 —— 36 个检查点。
不符即说明 J1 的臂构造或预热与 T1 不同源,结果不可用。

**为什么是 pv 而不是固定止损**:T1 重扫五窗实测,止损链净贡献 b3_c16(现值)+1.44、
b2_c26 −27.05,而退出构成显示主导项是 pv(IS×b2_c26:pv主动 398 vs 固定止损 81,4.9倍)。
固定止损轴留给 J2(前三名 × 5 档,条件搜索)。

用法: [BT_WORKERS=4] eff1_j1.py     (断点续跑:重复执行即可,已完成的臂-窗自动跳过)
"""
import importlib.util
import os
import sys
import time
from collections import Counter

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
os.environ.setdefault('MIN_TICKS', '3')
os.environ.setdefault('EFF1_LAYER', 'P1')          # 只为让 eff1_scan 能导入
os.environ['BT_WINDOWS'] = 'W1,W2,OOS,IS'          # 判定四窗,硬编码防误跑留出
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

RESULTS = RD + '/ablation/eff1_j1_results.txt'
JUDGE = ['W1', 'W2', 'OOS', 'IS']
BANDS, COUNTS = (2, 2.5, 3), (16, 22, 26)
PV_THR, PV_MULT = (-0.005, -0.01, -0.02), (3, 5)
BASE_THR, BASE_MULT = -0.01, 3                     # 生产现值 pv


def arm_name(b, c, t, m):
    return 'b%g_c%d_pv%g_m%d' % (b, c, t * 1000, m)


ARMS = [(arm_name(b, c, t, m), {'pv_thr': t, 'pv_mult': m}, {'band': b, 'count_min': c})
        for b in BANDS for c in COUNTS for t in PV_THR for m in PV_MULT]


def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


def done_set():
    d = set()
    try:
        for ln in open(RESULTS):
            if ln.startswith('J1/'):
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
    emit('== J1 几何×pv MIN_TICKS=%g workers=%d 开跑 %s (%d组合×%d窗) =='
         % (V2.MIN_TICKS, w_sim, time.strftime('%m-%d %H:%M'), len(ARMS), len(JUDGE)))
    done = done_set()
    for wn in JUDGE:
        s0, e0 = S.WD9[wn]
        todo = [a for a in ARMS if (wn, a[0]) not in done]
        if not todo:
            emit('[%s] SKIP' % wn); continue
        pool = pd.read_parquet(S.pool_path(wn))
        t0 = time.time()
        blocked = blocked_rts(cache, universe, pd.Timestamp(s0),
                              pd.Timestamp(e0) + pd.Timedelta(days=1), '1h', *SW.SHOCK,
                              min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
        lo = str((pd.Timestamp(s0) - pd.Timedelta(days=4)).date())
        hi = str((pd.Timestamp(e0) + pd.Timedelta(days=1)).date())
        base = S.make_picks(pool, 'K1', wn)
        tk = {sym: G.daily_tick(cache, sym, lo, hi)
              for sym in sorted({r['symbol'] for _, _, r in base})}
        emit('[%s] blocked=%d tick表=%d币 %.1fmin'
             % (wn, len(blocked), len(tk), (time.time() - t0) / 60))
        series = {}                                # 窗内共享,窗末释放
        by_geo = {}
        for a in todo:
            by_geo.setdefault((a[2]['band'], a[2]['count_min']), []).append(a)
        for (b, c), items in sorted(by_geo.items()):
            t0 = time.time()
            picks, drop = V2.tick_filter(S.make_picks(pool, 'K1', wn), tk,
                                         {'band': b, 'count_min': c})
            wd = V2.preload(cache, picks, wn, s0, e0, blocked, series)
            emit('[%s] b%g_c%d preload %.1fmin 格=%d 剔候选=%d'
                 % (wn, b, c, (time.time() - t0) / 60, len(wd.raw), drop))
            pv_cache = {}
            for name, chain_ov, geo_ov in items:
                t0 = time.time()
                ov = dict(chain_ov); ov.update(geo_ov)
                df = SW.run_arm(wd, SW.Arm('eff1', name, ov), pv_cache, workers=w_sim)
                m = SW.metrics(df, wd.days)
                nf = float(df['n_fills'].mean())
                er = Counter(df['exit_reason'])
                gate = ''
                if chain_ov['pv_thr'] == BASE_THR and chain_ov['pv_mult'] == BASE_MULT:
                    gate = '  [现值pv,须对上T1重扫 geo_b%g_c%d]' % (b, c)
                emit('J1/%s: %-22s ret%+9.2f mdd%6.2f calmar%10.4g fills%7.1f 格%d 破%d '
                     '%.1fmin | %s%s'
                     % (wn, name, m['ret'] * 100, -m['mdd'] * 100, m['calmar'], nf,
                        m['n_grids'], m['n_broke'], (time.time() - t0) / 60,
                        ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(2)),
                        gate))
            del wd
        del series
        emit('[%s] DONE' % wn)
    emit('J1_DONE')


if __name__ == '__main__':
    main()
