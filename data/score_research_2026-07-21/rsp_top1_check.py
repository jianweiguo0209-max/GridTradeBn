"""RSP111 top-1 选中一致性验证(2026-07-25,面板 vs 回放 的**输出级**比对)。

前置发现:面板与回放的 Reg_v2_5 完全一致、Sgcz_5 仅差 ≤3.3e-16(float64 1 ulp,
浮点求和顺序差异)。中间量等价不等于输出等价——rank(method='first') 对并列敏感,
故直接验证真正关心的东西:**每轮 RSP111 选中的 top-1 是不是同一个币**。
  100% 一致 → 走面板 join,复用上一战 8 窗 POOL(省 ~2h)
  有任何不一致 → 重跑 POOL 加列(同源)
RSP111 = rank(Reg_v2_5,asc)+rank(Sgcz_5,asc)+rank(p12_cross1,desc) 等权 method='first',
取 rs 最小;候选集与 p12 臂同口径(POOL 表 ∩ 有 p12 标签 ∩ 布网列非空)。
用法: rsp_top1_check.py [窗=HOLD-A] [抽样轮数=40]
"""
import contextlib
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import BT_FACTORS, BT_STRATEGY, BT_UNIVERSE_TOP_PCT
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import build_pit_candidates, load_full_series
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.grid_params import GRID_ROW_FACTORS
from gridtrade.core.selection import (compute_offset, needed_factors,
                                      proceed_calc_symbol_factor)
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
OUT = RD + '/ablation'
WD = {'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30')}
PANEL = {'HOLD-A': OUT + '/hold_factors_HOLD-A.parquet',
         'HOLD-B': OUT + '/hold_factors_HOLD-B.parquet'}
WN = sys.argv[1] if len(sys.argv) > 1 else 'HOLD-A'
NRT = int(sys.argv[2]) if len(sys.argv) > 2 else 40
RCOLS = ['Reg_v2_5', 'Sgcz_5']


def rsp_rank(d):
    """RSP111:三 rank 等权和,method='first';返回 rs 最小的 symbol。"""
    r = (d['Reg_v2_5'].rank(method='first', ascending=True)
         + d['Sgcz_5'].rank(method='first', ascending=True)
         + d['p12'].rank(method='first', ascending=False))
    return d.loc[r.idxmin(), 'symbol'], float(r.min())


def main():
    s0, e0 = WD[WN]
    pool = pd.read_parquet('%s/p12_pool_%s.parquet' % (OUT, WN))
    lab = pd.read_parquet('%s/hold_labels_%s.parquet' % (OUT, WN))[['rt', 'symbol', 'cross1']]
    lab = lab.rename(columns={'cross1': 'p12'})
    lab['rt'] = lab['rt'] + pd.Timedelta(hours=12)
    panel = pd.read_parquet(PANEL[WN])
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

    st = BT_STRATEGY
    needed = needed_factors(BT_FACTORS) | set(GRID_ROW_FACTORS) | set(RCOLS)
    rts = pd.date_range(ws, we, freq='1H')[:-1]
    step = max(1, len(rts) // NRT)
    rts = rts[::step][:NRT]
    devnull = open(os.devnull, 'w')
    tot = same = 0
    diffs = []
    for rt in rts:
        off = compute_offset(rt, st['period'])
        base = pool[(pool['rt'] == rt)]
        base = base[np.isfinite(base['close']) & np.isfinite(base['Atr_5'])
                    & np.isfinite(base['middle_5'])]
        base = base.merge(lab[lab['rt'] == rt][['symbol', 'p12']], on='symbol', how='inner')
        if len(base) < 5:
            continue
        # (a) 面板来源
        pn = panel[(panel['rt'] == rt) & (panel['offset'] == off)][['symbol'] + RCOLS]
        da = base.merge(pn, on='symbol', how='inner').dropna(subset=RCOLS)
        # (b) 回放来源
        cand = build_pit_candidates(series, rt, max_candle_num=st['max_candle_num'],
                                    min_quote_volume=0.0,
                                    top_volume_pct=BT_UNIVERSE_TOP_PCT, blacklist=bl)
        if not cand:
            continue
        with contextlib.redirect_stdout(devnull):
            all_df = proceed_calc_symbol_factor(cand, rt, st['period'], off,
                                                needed=needed, batch=True)
        if all_df is None or all_df.empty:
            continue
        all_df = all_df[(all_df['time'] + pd.to_timedelta(st['period'])) >= rt]
        db = base.merge(all_df[['symbol'] + RCOLS], on='symbol',
                        how='inner').dropna(subset=RCOLS)
        if len(da) < 5 or len(db) < 5:
            continue
        sa, ra = rsp_rank(da.reset_index(drop=True))
        sb, rb = rsp_rank(db.reset_index(drop=True))
        tot += 1
        if sa == sb:
            same += 1
        else:
            diffs.append((rt, off, len(da), len(db), sa, sb))
            print('  ✗ %s off=%d 面板选=%s 回放选=%s (n=%d/%d)'
                  % (rt, off, sa, sb, len(da), len(db)), flush=True)
    devnull.close()
    print('\n候选集规模一致性: 面板/回放 n 差异见上(应同)', flush=True)
    print('RSP111 top-1 一致: %d/%d (%.1f%%)'
          % (same, tot, 100.0 * same / max(tot, 1)), flush=True)
    print('结论: %s' % ('面板可用 → 走 join,复用现有 POOL' if same == tot and tot > 0
                        else '**须重跑 POOL 加列(同源)**'), flush=True)


if __name__ == '__main__':
    main()
