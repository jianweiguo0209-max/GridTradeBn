"""几何臂 × OOS 的**集中度**体检:剔 top-N 格 / 剔 top-N 币(2026-07-26)。

**问题**:eff1×geo_b2_c26 在 OOS 跑出 ret +35932%。已证不是缺陷(七假设全灭,见
blowup_equity_audit.py),但这个数是**全体均摊**还是**少数几格/几个币**撑起来的?

**已得结论(b2_c26)**:逐格层健康(max +63%、无一格过 +100%、80% 正格、剔 top-10 格只掉 18%),
但**逐币/逐 lane 层极度集中**——129 币里前 3 币扛 Σpnl 的 71%,12 lane 里 off0 一条扛 60%。

**做法**:跑一次取逐格 pnl_ratio,再按两种维度依次剔除后用 SW.metrics 重算组合。
lane 是严格串行单格流水线(每 (rt,offset) 槽恰好 1 格,已验),剔某格 = 该 lane 少复利一次。

**内存**:标准 preload 走 load_full_series(全史 1m × 全部币),在本机(16GB,常年颠簸)会与
扫描撞车 OOM。本脚本用 read_days_range 只读窗内天(+2 天余量供末轮持仓跨天),
holding_bars 取 [rt, rt+12h) ⇒ **bars 内容与全量切片逐位相同**,只是不读区间外的天。
⚠ 必须照抄 eff1_scan.preload 的 blocked_rts 冲击窗过滤,否则不是同一条臂(漏了会多 158 格)。

用法: [ARM=b2_c26|b2.5_c16|...] [WN=OOS] b2c26_trim_top.py
"""
import importlib.util
import os
import re
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import (BT_UNIVERSE_TOP_PCT, _FUNDING_BACK_MS,
                                             allocate_with_tiers, holding_bars)
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import CANDLE_COLS
from gridtrade.backtest.shock_replay import blocked_rts
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_s = importlib.util.spec_from_file_location('sc', RD + '/eff1_scan.py')
S = importlib.util.module_from_spec(_s)
_s.loader.exec_module(S)

WN = os.environ.get('WN', 'OOS')
ARM = os.environ.get('ARM', 'b2_c26')
BAND, CNT = re.match(r'b([\d.]+)_c(\d+)', ARM).groups()
GEO = {'band': float(BAND), 'count_min': int(CNT)}
TRIM_G = [0, 1, 3, 5, 10, 20, 50, 100]
TRIM_S = [0, 1, 2, 3, 5, 10, 20]


def build_wd(cache, s0, e0):
    """s0/e0 可以是**整窗**或**其中一段**(分段跑省内存,口径见 rsp2_is_split.py:
    两段各自只落逐格明细,merge 后按整窗 days 调 SW.metrics ⇒ 与整窗跑逐位一致,
    因为 metrics 只依赖逐格 run_time/offset/pnl_ratio)。"""
    pool = pd.read_parquet(S.pool_path(WN))
    picks = S.make_picks(pool, 'K1', WN)
    ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
    picks = [p for p in picks if ws <= p[0] < we]      # 按段切轮(整窗时为 no-op)
    universe = sorted(set(V.list_archive_symbols())
                      - set(effective_blacklist((), DEFAULT_TIER_POLICY)))
    blocked = blocked_rts(cache, universe, pd.Timestamp(s0),
                          pd.Timestamp(e0) + pd.Timedelta(days=1), '1h', *SW.SHOCK,
                          min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
    picks = [p for p in picks if p[0] not in blocked]
    picks, _ = allocate_with_tiers(picks, DEFAULT_TIER_POLICY, period=SW._S['period'])
    syms = sorted({row['symbol'] for _, _, row in picks})
    lo = str(pd.Timestamp(s0).date())
    hi = str((pd.Timestamp(e0) + pd.Timedelta(days=2)).date())
    series = {}
    for s in syms:                                    # 只读窗内天
        df = cache.read_days_range('1m', s, lo, hi)
        if df is None or df.empty:
            continue
        df = df[CANDLE_COLS].copy()
        df.sort_values('candle_begin_time', inplace=True)
        df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)
        series[s] = df.reset_index(drop=True)
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
            b0 = int(bars['candle_begin_time'].min().value // 1_000_000)
            b1 = int(bars['candle_begin_time'].max().value // 1_000_000)
            fd = fd[(fd['ts'] >= b0 - _FUNDING_BACK_MS) & (fd['ts'] <= b1)]
        raw.append((rt, int(offset), row, bars, fd, series[sym]))
    days = int((pd.Timestamp(e0) - pd.Timestamp(s0)).days) + 1
    return SW.WindowData(name=WN, start=pd.Timestamp(s0), end=pd.Timestamp(e0),
                         days=days, raw=raw, n_blocked=len(blocked), n_symbols=len(syms))


def report(df, days):
    p = df['pnl_ratio']
    print('\n===== 逐格 pnl_ratio 分布(n=%d) =====' % len(p))
    print('  mean%+.4f median%+.4f std%.4f min%+.4f max%+.4f  正格%.1f%%  >1.0 的格 %d'
          % (p.mean(), p.median(), p.std(), p.min(), p.max(), (p > 0).mean() * 100,
             (p > 1.0).sum()))
    print('\n===== 剔 top-N 格 =====')
    order = p.sort_values(ascending=False).index
    base = SW.metrics(df, days)['ret']
    print('  %-6s %-5s %13s %8s %10s' % ('剔N', '余格', 'ret%', 'MDD%', 'ret剩余'))
    for n in TRIM_G:
        m = SW.metrics(df.drop(index=order[:n]) if n else df, days)
        print('  %-6d %-5d %13.2f %8.2f %9.2f%%'
              % (n, len(df) - n, m['ret'] * 100, m['mdd'] * 100, m['ret'] / base * 100))
    print('\n===== 剔 top-N 币(按 Σpnl 降序) =====')
    s = df.groupby('symbol').agg(n=('pnl_ratio', 'size'), Σpnl=('pnl_ratio', 'sum'),
                                 均pnl=('pnl_ratio', 'mean'), 均fills=('n_fills', 'mean'))
    s['占比%'] = s['Σpnl'] / df['pnl_ratio'].sum() * 100
    s = s.sort_values('Σpnl', ascending=False)
    print(s.head(12).to_string(float_format=lambda x: '%.3f' % x))
    print('  币数=%d  前3币 %.1f%%  前5币 %.1f%%  前10币 %.1f%%'
          % (len(s), s['占比%'][:3].sum(), s['占比%'][:5].sum(), s['占比%'][:10].sum()))
    print('  %-6s %-5s %-5s %13s %8s %10s' % ('剔N币', '余币', '余格', 'ret%', 'MDD%', 'ret剩余'))
    for n in TRIM_S:
        keep = df[~df['symbol'].isin(s.index[:n])] if n else df
        if keep.empty:
            break
        m = SW.metrics(keep, days)
        print('  %-6d %-5d %-5d %13.2f %8.2f %9.2f%%'
              % (n, keep['symbol'].nunique(), len(keep), m['ret'] * 100,
                 m['mdd'] * 100, m['ret'] / base * 100))
    print('\n===== 12 lane 末值 =====')
    d = df.copy()
    d['close_ts'] = d['run_time'] + pd.to_timedelta(SW._S['period'])
    lanes = {int(o): float((1.0 + g['pnl_ratio']).prod())
             for o, g in d.sort_values('close_ts').groupby('offset')}
    tot = sum(lanes.values())
    top = sorted(lanes.items(), key=lambda kv: -kv[1])
    print('  ' + '  '.join('off%d=%.4g(%.0f%%)' % (o, v, v / tot * 100) for o, v in top[:4]))
    print('  最大单 lane 占 12 lane 之和 %.1f%%   最大/中位 %.0f 倍'
          % (top[0][1] / tot * 100,
             top[0][1] / sorted(lanes.values())[len(lanes) // 2]))


def main():
    SW.set_baseline({})
    s0, e0 = S.WD9[WN]
    out = '%s/ablation/%s_%s_grids.parquet' % (RD, ARM.replace('.', ''), WN)
    if os.path.exists(out):
        df = pd.read_parquet(out)
        days = int((pd.Timestamp(e0) - pd.Timestamp(s0)).days) + 1
        print('[cache] 复用 %s (格=%d)' % (os.path.basename(out), len(df)))
    else:
        cache = ParquetCache(V.default_cache_root())
        wd = build_wd(cache, s0, e0)
        print('[lean] %s/%s wd 格=%d 币=%d 天=%d'
              % (ARM, WN, len(wd.raw), wd.n_symbols, wd.days), flush=True)
        df = SW.run_arm(wd, SW.Arm('eff1', 'geo_' + ARM, dict(GEO)), {}, workers=2)
        df.to_parquet(out)
        days = wd.days
    m = SW.metrics(df, days)
    print('■ %s × %s: ret%+.2f%% MDD%.2f%% Calmar%.4g fills均%.1f'
          % (ARM, WN, m['ret'] * 100, m['mdd'] * 100, m['calmar'], m['n_fills']))
    report(df, days)


if __name__ == '__main__':
    main()
