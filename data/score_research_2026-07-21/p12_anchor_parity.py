"""锚保真度门(2026-07-25):证明"全池表重建的锚臂" ≡ "生产原路 preload_window"。

brief 红线:现役 rank_sum 臂必须走原路不动,锚不平停手查保真度。本战役为注入 p12
改了选币入口,故须先证:注入路径在 **ranker='rank'** 时与生产路径**逐位同**——
picks 集合、每格 (rt, offset, symbol)、布网输入 (close/Atr_5/middle_5)、bars 切片
全一致,且引擎跑出的 pnl 逐格相同。

小窗(默认 5 天)即可证伪:选币/cap/shock 的所有分支都在这几天里走过。
用法: .venv/bin/python data/score_research_2026-07-21/p12_anchor_parity.py [start] [end]
"""
import glob
import importlib.util
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_spec = importlib.util.spec_from_file_location('p12f', RD + '/p12_final_bt.py')
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

S0 = sys.argv[1] if len(sys.argv) > 1 else '2025-02-10'
E0 = sys.argv[2] if len(sys.argv) > 2 else '2025-02-14'
WN = 'PARITY'


def main():
    cache = ParquetCache(V.default_cache_root())
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    SW.set_baseline({})
    print('[parity] 窗 %s~%s' % (S0, E0), flush=True)

    print('[parity] A 生产原路 preload_window...', flush=True)
    wd_a = SW.preload_window(cache, universe, WN, S0, E0, workers=1, log=lambda *a: None)
    print('[parity]   格=%d 币=%d blocked=%d' % (len(wd_a.raw), wd_a.n_symbols,
                                                 wd_a.n_blocked), flush=True)

    print('[parity] B 注入路径(全池表 → ranker=rank)...', flush=True)
    for suf in ('.parquet', '.ckdone.txt'):        # 清上次 parity 产物,保证真跑
        p = '%s/p12_pool_%s%s' % (P.OUT, WN, suf)
        if os.path.exists(p):
            os.remove(p)
    for p in glob.glob('%s/p12_pool_%s.ckpart*.parquet' % (P.OUT, WN)):
        os.remove(p)
    P.build_pool(cache, WN, S0, E0)
    pool = pd.read_parquet('%s/p12_pool_%s.parquet' % (P.OUT, WN))
    n_rank = int(pool['rank'].notna().sum())
    print('[parity]   POOL表 行=%d(含过滤前全池) 其中有生产rank=%d'
          % (len(pool), n_rank), flush=True)
    picks = P.make_picks(pool, 'rank', WN)
    wd_b = P.preload_from_picks(cache, picks, WN, S0, E0, universe)
    print('[parity]   格=%d 币=%d blocked=%d' % (len(wd_b.raw), wd_b.n_symbols,
                                                 wd_b.n_blocked), flush=True)

    def key(wd):
        return [(str(rt), off, row['symbol']) for rt, off, row, _b, _f, _s in wd.raw]

    ka, kb = key(wd_a), key(wd_b)
    print('\n=== ① 格集合逐位 ===', flush=True)
    print('   n_A=%d n_B=%d 同=%s' % (len(ka), len(kb), ka == kb), flush=True)
    if ka != kb:
        sa, sb = set(ka), set(kb)
        print('   只在A:', sorted(sa - sb)[:5], flush=True)
        print('   只在B:', sorted(sb - sa)[:5], flush=True)

    print('=== ② 布网输入 + bars 切片 ===', flush=True)
    bad = 0
    for (rt_a, off_a, ra, ba, fa, _sa), (rt_b, off_b, rb, bb, fb, _sb) in zip(wd_a.raw, wd_b.raw):
        for c in ('close', 'Atr_5', 'middle_5'):
            if not np.isclose(float(ra[c]), float(rb[c]), rtol=0, atol=0):
                print('   差异 %s %s.%s: %r vs %r' % (rt_a, ra['symbol'], c,
                                                      ra[c], rb[c]), flush=True)
                bad += 1
        if len(ba) != len(bb) or not ba['close'].equals(bb['close']):
            print('   bars 差异 %s %s: len %d vs %d' % (rt_a, ra['symbol'],
                                                        len(ba), len(bb)), flush=True)
            bad += 1
        na = 0 if fa is None else len(fa)
        nb = 0 if fb is None else len(fb)
        if na != nb:
            print('   funding 差异 %s %s: %d vs %d' % (rt_a, ra['symbol'], na, nb), flush=True)
            bad += 1
    print('   不一致项=%d' % bad, flush=True)

    print('=== ③ 引擎逐格 pnl(s030 链) ===', flush=True)
    da = SW.run_arm(wd_a, SW.Arm('p12', 'anchor', {}), {}, workers=2)
    db = SW.run_arm(wd_b, SW.Arm('p12', 'anchor', {}), {}, workers=2)
    ma, mb = SW.metrics(da, wd_a.days), SW.metrics(db, wd_b.days)
    ca = da.sort_values(['run_time', 'symbol'])[['run_time', 'symbol', 'pnl_ratio',
                                                 'exit_reason']].reset_index(drop=True)
    cb = db.sort_values(['run_time', 'symbol'])[['run_time', 'symbol', 'pnl_ratio',
                                                 'exit_reason']].reset_index(drop=True)
    same = ca.equals(cb)
    print('   逐格明细同=%s | ret %+.4f vs %+.4f | calmar %.2f vs %.2f'
          % (same, ma['ret'] * 100, mb['ret'] * 100, ma['calmar'], mb['calmar']), flush=True)
    if not same and len(ca) == len(cb):
        d = ca['pnl_ratio'] - cb['pnl_ratio']
        print('   pnl 最大差=%.3e 差格数=%d' % (d.abs().max(), int((d.abs() > 0).sum())),
              flush=True)

    ok = (ka == kb) and bad == 0 and same
    print('\n锚保真度门: %s' % ('PASS(注入路径 ≡ 生产路径,可跑全窗)'
                                if ok else 'FAIL(停手查保真度)'), flush=True)


if __name__ == '__main__':
    main()
