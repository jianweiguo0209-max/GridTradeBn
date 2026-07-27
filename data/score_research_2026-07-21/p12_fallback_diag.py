"""同币 cap2 递补诊断(2026-07-25 用户问:"多少仓是选的非最优币?")。

allocate_with_tiers 按 rank 升序取**第一个未触顶**的币(tier2_cap=2 = 同币最多 2 个并发
持仓)。若 rank1 已触顶则顺延 rank2/rank3……→ 这就是"非最优币"。
  idx=0 → 选中本轮排名第一(最优)
  idx≥1 → 递补(非最优),idx 即让位层数
  empty  → top-K 候选全触顶,该轮不开仓
p12 臂 top-1 反复押同几个高 cross1 币 ⇒ 预期递补率显著高于锚臂(rank_sum 分散得多)。
复现战役完整前序:POOL 表 → (p12: join 标签重排) → shock 剔轮 → allocate_with_tiers。
用法: p12_fallback_diag.py [窗 ...]
"""
import importlib.util
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import BT_UNIVERSE_TOP_PCT, allocate_with_tiers
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.shock_replay import blocked_rts
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_spec = importlib.util.spec_from_file_location('p12f', RD + '/p12_final_bt.py')
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

WINS = sys.argv[1:] or ['HOLD-B', 'HOLD-A', 'W1', 'W2']


def main():
    cache = ParquetCache(V.default_cache_root())
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    SW.set_baseline({})
    rows = []
    for wn in WINS:
        s0, e0 = P.WD_ALL[wn]
        pool_p = '%s/p12_pool_%s.parquet' % (P.OUT, wn)
        try:
            pool = pd.read_parquet(pool_p)
        except FileNotFoundError:
            print('[skip] %s 无 POOL 表' % wn, flush=True)
            continue
        ws = pd.Timestamp(s0)
        we = pd.Timestamp(e0) + pd.Timedelta(days=1)
        blocked = blocked_rts(cache, universe, ws, we, '1h', *SW.SHOCK,
                              min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
        for ranker in ('rank', 'p12'):
            picks = P.make_picks(pool, ranker, wn)
            picks = [p for p in picks if p[0] not in blocked]
            n_rounds = len({(rt, off) for rt, off, _r in picks})
            kept, st = allocate_with_tiers(picks, DEFAULT_TIER_POLICY,
                                           period=SW._S['period'])
            fh = st['fallback_hist']
            n_fb = sum(fh.values())
            rows.append({
                '窗': wn, '臂': 'anchor' if ranker == 'rank' else 'p12',
                '有候选轮': n_rounds, '开仓': len(kept),
                '最优(idx0)': len(kept) - n_fb,
                '递补(idx≥1)': n_fb,
                '递补率%': round(100.0 * n_fb / max(len(kept), 1), 1),
                'idx1': fh.get(1, 0), 'idx2': fh.get(2, 0),
                'idx3+': sum(v for k, v in fh.items() if k >= 3),
                '空轮(全触顶)': st['empty_rounds'],
                '被拒候选': st['rejected_tier1'] + st['rejected_tier2'],
                '选中币数': len({r['symbol'] for _rt, _o, r in kept}),
            })
            print('  %s/%-6s 开仓%d 递补%d(%.1f%%) 空轮%d 币%d'
                  % (wn, rows[-1]['臂'], rows[-1]['开仓'], n_fb, rows[-1]['递补率%'],
                     st['empty_rounds'], rows[-1]['选中币数']), flush=True)
    d = pd.DataFrame(rows)
    print('\n===== 同币 cap2 递补诊断(tier2_cap=%s) ====='
          % getattr(DEFAULT_TIER_POLICY, 'tier2_cap', '?'), flush=True)
    print(d.to_string(index=False), flush=True)
    print('\n===== 汇总 =====', flush=True)
    for a in ('anchor', 'p12'):
        s = d[d['臂'] == a]
        if s.empty:
            continue
        print('%-7s 开仓合计=%d 递补=%d (%.1f%%) 空轮=%d'
              % (a, s['开仓'].sum(), s['递补(idx≥1)'].sum(),
                 100.0 * s['递补(idx≥1)'].sum() / max(s['开仓'].sum(), 1),
                 s['空轮(全触顶)'].sum()), flush=True)


if __name__ == '__main__':
    main()
