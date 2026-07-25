"""因子面板 vs 选币回放 一致性验证(2026-07-25 RSP111 战役前置决策)。

RSP111 需 Reg_v2_5/Sgcz_5,而现有 POOL 表只存了 rank/close/Atr_5/middle_5。两条路:
  (a) 从 hold_factors_*/sc_factors_* 面板 join —— 可复用上一战 8 窗 POOL 表(省 ~2h)
  (b) 改 POOL_COLS 重跑 POOL —— 同源保证,但 8 窗重跑
本脚本验证 (a) 是否安全:抽若干 rt,把选币回放现算的 all_df 与面板按 (rt,symbol,offset)
对齐,逐位比 Reg_v2_5/Sgcz_5。**逐位一致才允许走 (a)**,否则走 (b)。
面板 rt 语义 = candle_begin_time + 12h = 选币时刻(见 holdout_gate._factor_one)。
用法: rsp_panel_check.py [窗=HOLD-A]
"""
import contextlib
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import (BT_FACTORS, BT_STRATEGY, BT_UNIVERSE_TOP_PCT)
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import build_pit_candidates, load_full_series
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.grid_params import GRID_ROW_FACTORS
from gridtrade.core.selection import (compute_offset, needed_factors,
                                      proceed_calc_symbol_factor)
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
OUT = RD + '/ablation'
WD = {'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30'),
      'W1': ('2025-08-15', '2025-10-14'), 'IS': ('2026-03-01', '2026-06-30')}
PANEL = {'HOLD-A': OUT + '/hold_factors_HOLD-A.parquet',
         'HOLD-B': OUT + '/hold_factors_HOLD-B.parquet',
         'W1': RD + '/sc_factors_W1.parquet',
         'IS': RD + '/sc_factors_IS.parquet'}
WN = sys.argv[1] if len(sys.argv) > 1 else 'HOLD-A'
COLS = ['Reg_v2_5', 'Sgcz_5']


def main():
    s0, e0 = WD[WN]
    p = PANEL[WN]
    if not os.path.exists(p):
        print('[check] 面板不存在: %s' % p)
        return
    panel = pd.read_parquet(p)
    print('[check] %s 面板: 行=%d 币=%d 列=%s'
          % (WN, len(panel), panel['symbol'].nunique(), list(panel.columns)), flush=True)
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
    print('[check] 1h 有效币=%d' % len(series), flush=True)

    st = BT_STRATEGY
    needed = needed_factors(BT_FACTORS) | set(GRID_ROW_FACTORS) | set(COLS)
    rts = pd.date_range(ws, we, freq='1H')[:-1][::233][:6]
    devnull = open(os.devnull, 'w')
    tot = same = 0
    for rt in rts:
        off = compute_offset(rt, st['period'])
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
        # ⚠proceed_calc_symbol_factor 返回每币**多根历史 12H bar**;须先筛到当轮成型那根,
        # 否则 merge 出 symbol×time 笛卡尔积(破绽:n 会从 ~200 膨胀到数千)。
        # 与 build_pool / _select_over_run_times 同口径。
        all_df = all_df[(all_df['time'] + pd.to_timedelta(st['period'])) >= rt]
        if all_df.empty:
            continue
        pn = panel[(panel['rt'] == rt) & (panel['offset'] == off)]
        m = all_df.merge(pn, on='symbol', suffixes=('_r', '_p'))
        if m.empty:
            print('  %s off=%d 面板无对应行(rt/offset 对不上)' % (rt, off), flush=True)
            continue
        ok = True
        for c in COLS:
            a, b = m[c + '_r'].values.astype(float), m[c + '_p'].values.astype(float)
            eq = np.isclose(a, b, rtol=0, atol=0, equal_nan=True)
            if not eq.all():
                d = np.abs(a - b)[~eq]
                print('  %s off=%d %s: 不一致 %d/%d 最大差=%.3g'
                      % (rt, off, c, (~eq).sum(), len(eq), np.nanmax(d)), flush=True)
                ok = False
        tot += 1
        same += int(ok)
        print('  %s off=%d n=%d 逐位一致=%s' % (rt, off, len(m), ok), flush=True)
    devnull.close()
    print('\n结论: %d/%d 轮逐位一致 → %s'
          % (same, tot, '面板可用,走 join 路线(复用现有 POOL)'
             if same == tot and tot > 0 else '面板不可信,须重跑 POOL 加列'), flush=True)


if __name__ == '__main__':
    main()
