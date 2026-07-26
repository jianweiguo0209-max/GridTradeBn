"""网格间距 vs 交易所最小报价单位(tick)—— 引擎的价格分辨率盲区(2026-07-26)。

**发现经过**:查 b2_c26×OOS 的 top-1 格(FLOW,2026-01-27 11:00~23:00,pnl+63.4%,1020 笔)
时发现:该 12H 内 FLOW 价格**只有 4 个不同取值** [0.071 0.072 0.073 0.074] ⇒ tick=0.001,
而网格间距是 0.000726 ⇒ **间距/tick = 0.726**,网格线比交易所报价精度还密,
区间内 4 条线里只有 0.072 是真实存在的价位,另外三条**挂不上单**。

**引擎侧事实**:
  · `trans_candle_to_tick` / `grid_touch_info` 把价格当**连续量**,无任何 tick 量化
  · 守卫 `grid_spacing_min=0.003` 是**相对价格**的(0.3%),**没有任何相对 tick 的守卫**
    ——FLOW 的 tick 就值 1.06%,间距 1.0088% 远高于 0.3% 故守卫不响
**实盘侧事实**:
  · `ccxt_adapter.create_limit_order` 内部 `quantize_price` 按 tickSize 量化(line 315),
    有 `-1111` 拒单事故先例(tickSize=1e-05 实证 11/11 价超精度 → 开格零挂单卡 OPENING)
  · ⇒ 实盘会把多条网格线**坍缩到同一价位**,realized 几何 ≠ nominal 几何

**⚠ 本脚本只量暴露面,不断定失真的方向与幅度。** 亚 tick 线被量化后等价于"在该价位挂更大的
单",一次 tick 跳动同时吃掉这些线,盈亏两侧同时缩放,净效应方向需实测。用 CELO 反推
(290 笔 × 1 tick 0.88% × 单线名义占比 0.127 ≈ 16% of cap,实际均 pnl 11.5%)量级反而自洽。
⇒ **决定性检验 = 给引擎加"网格线按 tick 量化 + 同价位合并"后重跑**(改生产引擎,须重验九窗锚)。

**tick 推断口径**:必须**逐格在它自己的 12H 窗内**取"全部不同 OHLC 价格的最小正差"。
用全窗(两个月)的最小正差会被单个异常值压塌 —— 实错:FLOW 全窗推出 spt=2215,
与单格直接观测的 0.73 自相矛盾(见 [[silent-sample-drop-measurement-traps]] 同类)。

用法: [ARM=b2_c26] [WN=OOS] tick_resolution_audit.py   (需先有 <ARM>_<WN>_grids.parquet)
"""
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
ARM = os.environ.get('ARM', 'b2_c26')
WN = os.environ.get('WN', 'OOS')
GEOS = [(2, 26, 'b2_c26'), (2, 16, 'b2_c16'), (2.5, 16, 'b2.5_c16'), (3, 16, 'b3_c16')]


def main():
    cache = ParquetCache(V.default_cache_root())
    d = pd.read_parquet('%s/ablation/%s_%s_grids.parquet' % (RD, ARM, WN))
    d['spacing'] = (d['high'] - d['low']) / d['grid_num']
    lo = str((pd.Timestamp(d['run_time'].min()) - pd.Timedelta(days=1)).date())
    hi = str((pd.Timestamp(d['run_time'].max()) + pd.Timedelta(days=2)).date())
    m1c, ticks, npx = {}, [], []
    for r in d.itertuples(index=False):
        if r.symbol not in m1c:
            m1c[r.symbol] = cache.read_days_range('1m', r.symbol, lo, hi)
        m = m1c[r.symbol]
        rt = pd.Timestamp(r.run_time)
        b = m[(m['candle_begin_time'] >= rt) & (m['candle_begin_time'] < rt + pd.Timedelta('12H'))]
        if b.empty:
            ticks.append(np.nan)
            npx.append(0)
            continue
        px = np.unique(np.concatenate([b['open'], b['high'], b['low'], b['close']]))
        dd = np.diff(px)
        dd = dd[dd > 0]
        ticks.append(float(dd.min()) if len(dd) else np.nan)
        npx.append(len(px))
    d['tick'], d['n_px'] = ticks, npx
    d = d.dropna(subset=['tick'])
    d['spt'] = d['spacing'] / d['tick']
    tot = d['pnl_ratio'].sum()
    print('%s × %s  可判定格 %d(逐格在自己 12H 窗内推 tick)' % (ARM, WN, len(d)))
    print('\n间距/tick 分位: ' + '  '.join('q%.2f=%.2f' % (q, d['spt'].quantile(q))
                                        for q in (.01, .05, .1, .25, .5, .75, .9)))
    for thr in (1.0, 2.0, 3.0, 5.0):
        m = d['spt'] < thr
        print('  间距 < %.0f tick: %4d 格 (%.1f%%)  占 Σpnl %.1f%%  均 fills=%.0f'
              % (thr, m.sum(), m.mean() * 100, d[m]['pnl_ratio'].sum() / tot * 100,
                 d[m]['n_fills'].mean()))
    print('\n该 12H 内价格的不同取值个数(网格再密也只能停在这些价位上):')
    for thr in (5, 10, 20, 50):
        m = d['n_px'] <= thr
        print('  ≤%2d 个价位: %4d 格 (%.1f%%)  占 Σpnl %.1f%%'
              % (thr, m.sum(), m.mean() * 100, d[m]['pnl_ratio'].sum() / tot * 100))
    t = d.groupby('symbol').agg(n=('pnl_ratio', 'size'), Σpnl=('pnl_ratio', 'sum'),
                                tick=('tick', 'median'), spt=('spt', 'median'),
                                n_px=('n_px', 'median'), fills=('n_fills', 'mean'))
    print('\n前 6 大贡献币:')
    print(t.nlargest(6, 'Σpnl').to_string(float_format=lambda x: '%.4f' % x))
    b0, c0 = [(b, c) for b, c, n in GEOS if n == ARM][0]
    print('\n各几何暴露(同批格几何缩放 spacing ∝ band/count):')
    for band, cnt, lab in GEOS:
        v = d['spt'] * (band / float(b0)) * (float(c0) / cnt)
        print('  %-10s 间距/tick median=%5.2f | <1: %4.1f%%格 | <2: %4.1f%%格 | <3: %4.1f%%格'
              % (lab, v.median(), (v < 1).mean() * 100, (v < 2).mean() * 100,
                 (v < 3).mean() * 100))


if __name__ == '__main__':
    main()
