"""1m 缺口 × top55% 量筛 交互诊断(2026-07-25 用户问:"缺数据和这个过滤有关系吗?")。

链路事实:top55% 量筛只读 1h(vol24=前24根1h的quote_volume和),1m 只在池选定后开格才用。
故票池构成不受 1m 缺失污染;真问题=**缺 1m 的币是否恰好是会被 top55% 刷掉的低量币**。
  若是 → 缺口被量筛大幅缓解,补全影响小;
  若否 → 缺 1m 的币真进了池,补全影响真实。
口径:补全前"有 1m"的币集 = .prebf 标签的 symbol(cross1 读 1m 算,有标签⇔当时有1m)。
用法: p12_gap_diag.py [窗名=HOLD-B]
"""
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import BT_STRATEGY, BT_UNIVERSE_TOP_PCT
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import build_pit_candidates, load_full_series
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

OUT = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21/ablation'
WD = {'HOLD-B': ('2024-10-01', '2024-11-30'), 'HOLD-A': ('2025-02-01', '2025-03-31')}
WN = sys.argv[1] if len(sys.argv) > 1 else 'HOLD-B'


def main():
    s0, e0 = WD[WN]
    old = set(pd.read_parquet('%s/hold_labels_%s.prebf.parquet' % (OUT, WN))['symbol'])
    print('[diag] %s 补全前有 1m 的币=%d' % (WN, len(old)), flush=True)
    cache = ParquetCache(V.default_cache_root())
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    syms = sorted(set(V.list_archive_symbols()) - set(bl))
    series = load_full_series(cache, syms, '1h')
    ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
    lo = ws - pd.Timedelta(days=10)
    for s_ in list(series):
        df = series[s_]
        df = df[(df['candle_begin_time'] >= lo) & (df['candle_begin_time'] < we)]
        if len(df) < 24:
            del series[s_]
        else:
            series[s_] = df.reset_index(drop=True)
    print('[diag] 1h 有效币=%d' % len(series), flush=True)

    rts = pd.date_range(ws, we, freq='1H')[:-1]
    rts = rts[::73]                       # 抽样 ~20 轮
    rows = []
    for rt in rts:
        # 量筛前的全体 eligible(top_volume_pct=0 → 不截断),用于定位截断线
        full = build_pit_candidates(series, rt, max_candle_num=BT_STRATEGY['max_candle_num'],
                                    min_quote_volume=0.0, top_volume_pct=0.0, blacklist=bl)
        if not full:
            continue
        kept = build_pit_candidates(series, rt, max_candle_num=BT_STRATEGY['max_candle_num'],
                                    min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT,
                                    blacklist=bl)
        n_full, n_kept = len(full), len(kept)
        k_old = sum(1 for s_ in kept if s_ in old)
        f_old = sum(1 for s_ in full if s_ in old)
        # 量分位:新增币(补全带来的)在池内的量排名分布
        vols = {s_: float(series[s_][series[s_]['candle_begin_time'] < rt]
                          .tail(24)['quote_volume'].sum()) for s_ in full}
        sv = sorted(vols.items(), key=lambda kv: -kv[1])
        rank_of = {s_: i for i, (s_, _v) in enumerate(sv)}
        new_ranks = [rank_of[s_] / max(n_full - 1, 1) for s_ in full if s_ not in old]
        old_ranks = [rank_of[s_] / max(n_full - 1, 1) for s_ in full if s_ in old]
        rows.append({
            'rt': rt, 'n_full': n_full, 'n_kept': n_kept,
            'kept_有1m': k_old, 'kept_缺1m': n_kept - k_old,
            'full_缺1m': n_full - f_old,
            '缺1m币量分位中位': np.median(new_ranks) if new_ranks else np.nan,
            '有1m币量分位中位': np.median(old_ranks) if old_ranks else np.nan,
        })
    d = pd.DataFrame(rows)
    print('\n===== 逐轮(抽样 %d 轮) =====' % len(d), flush=True)
    print(d.to_string(index=False), flush=True)
    print('\n===== 汇总 =====', flush=True)
    print('量筛前池 n_full 均值      : %.0f' % d['n_full'].mean())
    print('量筛后池 n_kept 均值      : %.0f (top%.0f%%)'
          % (d['n_kept'].mean(), BT_UNIVERSE_TOP_PCT * 100))
    print('  其中 补全前就有1m       : %.0f (%.1f%%)'
          % (d['kept_有1m'].mean(), 100 * d['kept_有1m'].mean() / d['n_kept'].mean()))
    print('  其中 当时缺1m(会丢格)  : %.0f (%.1f%%)'
          % (d['kept_缺1m'].mean(), 100 * d['kept_缺1m'].mean() / d['n_kept'].mean()))
    print('量分位(0=量最大,1=量最小,量筛线=%.2f):' % BT_UNIVERSE_TOP_PCT)
    print('  缺1m币 中位分位: %.3f' % d['缺1m币量分位中位'].mean())
    print('  有1m币 中位分位: %.3f' % d['有1m币量分位中位'].mean())
    r = d['kept_缺1m'].mean() / d['n_kept'].mean()
    print('\n结论: 量筛后池中"当时缺1m"占比 %.1f%% → %s'
          % (100 * r, '量筛已大幅缓解,补全影响有限' if r < 0.05
             else '缺1m的币确实进了池,补全影响真实'))


if __name__ == '__main__':
    main()
