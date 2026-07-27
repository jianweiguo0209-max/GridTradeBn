"""tick 闸门套九窗:哪些臂-窗读数还站得住?(2026-07-26)

**闸门依据**(见 engine-no-tick-quantization-blindspot / tick_quantize_compare 实测):
  间距 < 1 tick   → 相邻网格线量化到同一价位,几何**物理上不可实现**;实盘 _replenish_opposite
                    按线序号补对侧单 ⇒ 约 31% 的 round trip 退化成同价买卖(毛利 0、倒贴手续费)
  间距 < 10 tick  → 量化抖动 ≈ tick/spacing > 10%,回测两种建模(off/stack)不收敛:
                    实测 OOS 上 off→stack 成交数暴涨 4.5~10.4×,三臂 ret 全部离谱 ⇒ 读数不可用
  间距 ≥ 10 tick  → 抖动 <10%,off ≡ stack;实盘现值最小 23 tick、中位 133,落在此区

**tick 口径**:逐 (币,日) 从 **1h** 归档推(当日全部不同 OHLC 价格的最小正差,取 ±3 日窗内最小,
robust 到"当日价位太少"的高估)。1h 已对 1m 校验:289 币 **98.6% 完全相等**,tick 最粗的 15 币
(风险区)100% 精确;少数高估会让闸门更保守,方向安全。
⚠ 不可用"全窗最小差"——单个异常值会把它压塌(实错见 silent-sample-drop-measurement-traps)。

用法: tick_gate_ninewin.py
"""
import importlib.util
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
os.environ.setdefault('WN', 'OOS')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.core.grid_params import calc_grid_params_v2

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_s = importlib.util.spec_from_file_location('sc', RD + '/eff1_scan.py')
S = importlib.util.module_from_spec(_s)
_s.loader.exec_module(S)

GEOS = [(b, c, 'b%g_c%d' % (b, c)) for b in (2, 2.5, 3) for c in (16, 22, 26)]
HARD, SOFT = 1.0, 10.0          # <1 tick 不可实现;<10 tick 存疑


def daily_tick(cache, sym, lo, hi):
    """{date: tick} —— 1h 逐日最小正差,再取 ±3 日窗内最小(治"当日价位太少"的高估)。"""
    d = cache.read_days_range('1h', sym, lo, hi)
    if d is None or d.empty:
        return None
    by = {}
    for day, g in d.groupby(d['candle_begin_time'].dt.date):
        px = np.unique(np.concatenate([g['open'], g['high'], g['low'], g['close']]))
        dd = np.diff(px)
        dd = dd[dd > 1e-12]
        if len(dd):
            by[day] = float(dd.min())
    if not by:
        return None
    s = pd.Series(by).sort_index()
    return s.rolling(7, center=True, min_periods=1).min().to_dict()


def main():
    SW.set_baseline({})
    cache = ParquetCache(V.default_cache_root())
    out = []
    for wn, (s0, e0) in S.WD9.items():
        try:
            pool = pd.read_parquet(S.pool_path(wn))
        except Exception:
            continue
        picks = S.make_picks(pool, 'K1', wn)
        lo = str((pd.Timestamp(s0) - pd.Timedelta(days=4)).date())
        hi = str((pd.Timestamp(e0) + pd.Timedelta(days=1)).date())
        tk = {}
        for sym in sorted({r['symbol'] for _, _, r in picks}):
            tk[sym] = daily_tick(cache, sym, lo, hi)
        rows = []
        for rt, _off, row in picks:
            m = tk.get(row['symbol'])
            if not m:
                continue
            t = m.get(pd.Timestamp(rt).date())
            if not t or t != t:
                continue
            for band, cnt, lab in GEOS:
                v2 = dict(SW._V2, atr_range_multiplier=band, grid_count_min=cnt,
                          grid_spacing_max=SW.baseline()['spacing_max'])
                try:
                    p = calc_grid_params_v2(row=row, price_limit=SW._S['price_limit'],
                                            stop_limit=SW._S['stop_limit'], v2_config=v2)
                except Exception:
                    continue
                sp = (p['high_price'] - p['low_price']) / p['grid_count']
                rows.append({'win': wn, 'geo': lab, 'spt': sp / t})
        if rows:
            out.append(pd.DataFrame(rows))
            print('  %-8s 格=%d 币=%d' % (wn, len(picks), len(tk)), flush=True)
    d = pd.concat(out, ignore_index=True)
    d.to_parquet(RD + '/ablation/tick_gate_ninewin.parquet')
    W = [w for w in S.WD9 if w in set(d['win'])]
    for thr, name in ((HARD, '<1 tick 不可实现'), (SOFT, '<10 tick 存疑')):
        print('\n===== %s 的格占比 =====' % name)
        print('%-10s %s' % ('臂', ' '.join('%8s' % w for w in W)))
        for _b, _c, lab in GEOS:
            cells = []
            for w in W:
                g = d[(d['win'] == w) & (d['geo'] == lab)]
                cells.append('%7.1f%%' % ((g['spt'] < thr).mean() * 100) if len(g) else '     — ')
            print('%-10s %s' % (lab, ' '.join(cells)))
    print('\n===== 闸门裁决(<10 tick 占比 ≤5%% 判可用) =====')
    print('%-10s %s  可用窗数' % ('臂', ' '.join('%8s' % w for w in W)))
    for _b, _c, lab in GEOS:
        cells, n = [], 0
        for w in W:
            g = d[(d['win'] == w) & (d['geo'] == lab)]
            if not len(g):
                cells.append('     — ')
                continue
            f = (g['spt'] < SOFT).mean()
            ok = f <= 0.05
            n += ok
            cells.append('%8s' % ('可用' if ok else ('剔除' if f > 0.30 else '存疑')))
        print('%-10s %s      %d/%d' % (lab, ' '.join(cells), n, len(W)))


if __name__ == '__main__':
    main()
