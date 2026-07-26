"""留出一级:J1 终点 vs 现值锚 × HOLD-A~E —— 预注册 2026-07-26-geo-chain-joint §4。

**臂(只有两个,不得增加)**:
  终点 `b2_c26_pv-5_m5`  = band2 × count26 × pv_thr −0.5% × pv_mult 5
  现值锚 `b3_c16_pv-10_m3` = 生产现值(band3 × count16 × pv 现值)
**窗**:HOLD-A/B/C/D/E 五窗。**HOLD-F/JUL26 是处女终审窗,本层禁止触碰**(§5/§6)。

**判据(§4,写死不挪)**——三条全过才进终审:
  (i)   五窗中至少 3 窗 ret 优于现值同窗
  (ii)  五窗合计 ret 优于现值合计 **且领先 ≥ 2pp**
  (iii) 每窗 MDD ≤ 现值同窗 MDD × 1.3
一级定位:**灾难滤网,不是统计裁决**;过关不构成优越性证据,只表示未被当场证伪。

**内建字节门**:现值锚在五窗必须逐位复现 T1 重扫 `P1/HOLD-*: geo_b3_c16`
(J1 已在判定四窗过同款门 36/36)。不符即预热/臂构造漂移,结果作废。

**预测(写在出数之前)**:同族 `b2_c26_pv-10_m3` 在 T1 留出五窗 = **−4.77**
(逐窗 +2.34 +1.82 −4.16 −8.16 +3.39),故预测终点 FAIL。预测不替代检验。

用法: [BT_WORKERS=4] eff1_h1.py    (断点续跑)
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

RESULTS = RD + '/ablation/eff1_h1_results.txt'
HOLD = ['HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E']   # 硬编码:处女终审窗跑不到
ARMS = [('END_b2_c26_pv-5_m5', {'pv_thr': -0.005, 'pv_mult': 5},
         {'band': 2, 'count_min': 26}),
        ('ANCHOR_b3_c16_pv-10_m3', {'pv_thr': -0.01, 'pv_mult': 3},
         {'band': 3, 'count_min': 16})]
assert not ({'HOLD-F', 'JUL26'} & set(HOLD)), '处女终审窗禁入'


def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


def done_set():
    d = set()
    try:
        for ln in open(RESULTS):
            if ln.startswith('H1/'):
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
    emit('== H1 留出一级 MIN_TICKS=%g workers=%d 开跑 %s (2臂×5窗) =='
         % (V2.MIN_TICKS, w_sim, time.strftime('%m-%d %H:%M')))
    done = done_set()
    for wn in HOLD:
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
        series = {}
        for name, chain_ov, geo_ov in todo:
            t0 = time.time()
            picks, drop = V2.tick_filter(S.make_picks(pool, 'K1', wn), tk, geo_ov)
            wd = V2.preload(cache, picks, wn, s0, e0, blocked, series)
            ov = dict(chain_ov); ov.update(geo_ov)
            df = SW.run_arm(wd, SW.Arm('eff1', name, ov), {}, workers=w_sim)
            m = SW.metrics(df, wd.days)
            er = Counter(df['exit_reason'])
            gate = '  [锚,须对上T1 P1/%s geo_b3_c16]' % wn if name.startswith('ANCHOR') else ''
            emit('H1/%s: %-24s ret%+9.2f mdd%6.2f calmar%10.4g fills%7.1f 格%d 破%d 剔%d '
                 '%.1fmin | %s%s'
                 % (wn, name, m['ret'] * 100, -m['mdd'] * 100, m['calmar'],
                    float(df['n_fills'].mean()), m['n_grids'], m['n_broke'], drop,
                    (time.time() - t0) / 60,
                    ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(2)), gate))
            del wd
        del series
        emit('[%s] DONE' % wn)
    emit('H1_DONE')


if __name__ == '__main__':
    main()
