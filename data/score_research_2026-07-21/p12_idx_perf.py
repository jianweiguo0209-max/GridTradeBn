"""按 cap2 递补层数(idx)分层看表现(2026-07-25 用户令"按 idx 分开计算表现做对比")。

背景:递补诊断显示 p12 臂 80.7% 的仓位不是 p12 top-1,而是 cap2 触顶后顺延的 top-2~5。
于是关键问题变成:**p12 排名本身有没有单调性?**
  idx0 显著优于 idx3+ → 排名有区分度,top-1 信号真实,cap2 是在稀释一个真边;
  各层表现雷同         → 排名无区分度,组合收益来自别处(如"高波动币池"本身),
                         被探针验证过的 top-1 配对 alpha 在组合级并未兑现。
做法:复刻 allocate_with_tiers 但记录每格的 idx(让位层数)→ 塞进 row → 跑引擎取逐格
pnl → 按 idx 分层统计。锚臂同样处理作对照。
用法: p12_idx_perf.py [窗 ...] [--arm p12_s030]
"""
import heapq
import importlib.util
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist, pick_first_allowed

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_spec = importlib.util.spec_from_file_location('p12f', RD + '/p12_final_bt.py')
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

ARGS = [a for a in sys.argv[1:] if not a.startswith('--')]
WINS = ARGS or ['HOLD-B', 'W1']
ARM = 'p12_s030'
for i, a in enumerate(sys.argv):
    if a == '--arm':
        ARM = sys.argv[i + 1]
ARM_MAP = {n: (rk, ov) for n, rk, ov in P.ARMS}   # 臂名 → (选币器, 链覆盖)


def allocate_with_idx(ranked_picks, tiers, period='12H'):
    """allocate_with_tiers 同逻辑,额外把让位层数写进 row['fb_idx']。"""
    td = pd.to_timedelta(period)
    by_round = {}
    for rt, off, row in ranked_picks:
        by_round.setdefault((rt, off), []).append((rt, off, row))
    expiry, held, kept = [], {}, []
    for key in sorted(by_round):
        rt = key[0]
        while expiry and expiry[0][0] <= rt:
            _, sym = heapq.heappop(expiry)
            held[sym] -= 1
            if not held[sym]:
                del held[sym]
        cands = sorted(by_round[key], key=lambda t: t[2]['rank'])
        idx = pick_first_allowed([c[2]['symbol'] for c in cands], held, tiers)
        if idx is None:
            continue
        chosen = cands[idx]
        row = chosen[2].copy()
        row['fb_idx'] = int(idx)
        sym = row['symbol']
        held[sym] = held.get(sym, 0) + 1
        heapq.heappush(expiry, (rt + td, sym))
        kept.append((chosen[0], chosen[1], row))
    return kept


def run_window(cache, universe, wn, arm):
    ranker, ov = ARM_MAP[arm]
    s0, e0 = P.WD_ALL[wn]
    pool = pd.read_parquet('%s/p12_pool_%s.parquet' % (P.OUT, wn))
    picks = P.make_picks(pool, ranker, wn)
    ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
    from gridtrade.backtest.backtest_run import BT_UNIVERSE_TOP_PCT
    from gridtrade.backtest.shock_replay import blocked_rts
    blocked = blocked_rts(cache, universe, ws, we, '1h', *SW.SHOCK,
                          min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
    picks = [p for p in picks if p[0] not in blocked]
    picks = allocate_with_idx(picks, DEFAULT_TIER_POLICY, period=SW._S['period'])
    idx_map = {(pd.Timestamp(rt), r['symbol']): int(r['fb_idx']) for rt, _o, r in picks}
    # 复用战役的 1m 装载/切窗(picks 已定,直接走后半段)
    wd = P._preload_core(cache, picks, wn, s0, e0) if hasattr(P, '_preload_core') else None
    if wd is None:
        wd = _preload(cache, picks, wn, s0, e0)
    df = SW.run_arm(wd, SW.Arm('p12', arm, ov), {}, workers=3)
    df['fb_idx'] = [idx_map.get((pd.Timestamp(rt), s), -1)
                    for rt, s in zip(df['run_time'], df['symbol'])]
    return df


def _preload(cache, picks, wn, s0, e0):
    """= p12_final_bt.preload_from_picks 的后半段(picks 已含 idx,不再重跑 allocate)。"""
    from gridtrade.backtest import selection_replay as SR
    from gridtrade.backtest.backtest_run import _FUNDING_BACK_MS, holding_bars
    ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
    syms = sorted({row['symbol'] for _, _, row in picks})
    series = SR.load_full_series(cache, syms, '1m')
    fmap, raw = {}, []
    for rt, offset, row in picks:
        sym = row['symbol']
        if sym not in series:
            continue
        bars = holding_bars(series[sym], rt, SW._S['period'])
        if len(bars) == 0:
            continue
        if sym not in fmap:
            fmap[sym] = cache.read_all_days('funding', sym)
        fd = fmap[sym]
        if fd is not None and not fd.empty:
            lo = int(bars['candle_begin_time'].min().value // 1_000_000)
            hi = int(bars['candle_begin_time'].max().value // 1_000_000)
            fd = fd[(fd['ts'] >= lo - _FUNDING_BACK_MS) & (fd['ts'] <= hi)]
        raw.append((rt, int(offset), row, bars, fd, series[sym]))
    days = int((pd.Timestamp(e0) - pd.Timestamp(s0)).days) + 1
    return SW.WindowData(name=wn, start=ws, end=we, days=days, raw=raw,
                         n_blocked=0, n_symbols=len(syms))


def report(df, wn, arm):
    d = df[df['fb_idx'] >= 0].copy()
    d['层'] = d['fb_idx'].apply(lambda i: 'idx%d' % i if i <= 2 else 'idx3+')
    print('\n===== %s / %s (n=%d) =====' % (wn, arm, len(d)), flush=True)
    print('%-7s %6s %10s %10s %8s %10s %10s'
          % ('层', '格数', '均值bp', '中位bp', '胜率', 'p5bp', 'p95bp'))
    for lv in ('idx0', 'idx1', 'idx2', 'idx3+'):
        g = d[d['层'] == lv]
        if g.empty:
            continue
        p = g['pnl_ratio']
        tr = g[g['exit_reason'] != '未触网']['pnl_ratio']
        print('%-7s %6d %10.1f %10.1f %8.3f %10.1f %10.1f'
              % (lv, len(g), p.mean() * 1e4, p.median() * 1e4,
                 float((tr > 0).mean()) if len(tr) else np.nan,
                 p.quantile(0.05) * 1e4, p.quantile(0.95) * 1e4))
    a = d[d['fb_idx'] == 0]['pnl_ratio']
    b = d[d['fb_idx'] > 0]['pnl_ratio']
    if len(a) > 5 and len(b) > 5:
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        t = (a.mean() - b.mean()) / se if se > 0 else np.nan
        print('  idx0 vs 递补: Δ均值=%+.1fbp  t=%+.2f  %s'
              % ((a.mean() - b.mean()) * 1e4, t,
                 '排名有区分度' if abs(t) >= 2 else '**无显著区分度**'))
    return d


def main():
    cache = ParquetCache(V.default_cache_root())
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    SW.set_baseline({})
    alld = []
    for wn in WINS:
        df = run_window(cache, universe, wn, ARM)
        alld.append(report(df, wn, ARM))
    if len(alld) > 1:
        d = pd.concat(alld, ignore_index=True)
        report_all = d.copy()
        print('\n===== 合并(%s) =====' % '+'.join(WINS), flush=True)
        print('%-7s %6s %10s %10s %8s' % ('层', '格数', '均值bp', '中位bp', '胜率'))
        for lv in ('idx0', 'idx1', 'idx2', 'idx3+'):
            g = report_all[report_all['层'] == lv]
            if g.empty:
                continue
            p = g['pnl_ratio']
            tr = g[g['exit_reason'] != '未触网']['pnl_ratio']
            print('%-7s %6d %10.1f %10.1f %8.3f'
                  % (lv, len(g), p.mean() * 1e4, p.median() * 1e4,
                     float((tr > 0).mean()) if len(tr) else np.nan))
        a = report_all[report_all['fb_idx'] == 0]['pnl_ratio']
        b = report_all[report_all['fb_idx'] > 0]['pnl_ratio']
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        t = (a.mean() - b.mean()) / se if se > 0 else np.nan
        print('  合并 idx0 vs 递补: Δ=%+.1fbp t=%+.2f %s'
              % ((a.mean() - b.mean()) * 1e4, t,
                 '排名有区分度' if abs(t) >= 2 else '**无显著区分度**'))


if __name__ == '__main__':
    main()
