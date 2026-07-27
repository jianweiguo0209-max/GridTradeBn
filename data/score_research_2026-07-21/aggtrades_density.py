"""第二步(决定性):沿**密度轴**外推 —— 同一批真实 aggTrades 路径,人工收紧间距。

仪器已在已知点校准(aggtrades_validate.py:X_real/实盘真值=1.0058,逐格 97.5% 精确,
X_true/X_engine=0.9970)。现在保持每格的 band[low,high] 与 entry 不变,只改 grid_count
以扫出目标间距,在**同一条真实价格路径**上重算三数:

  X_engine  1m→4tick 近似(回测算的)  X_true 真实逐笔穿越  X_real 真实逐笔+executor(5s补单)

关键比值随密度的走向:
  X_true / X_engine  —— 4-tick 三段折线漏掉了多少真实分钟内往返(密度↑ 应 >1 且放大)
  X_real / X_true    —— 5s 补单延迟吃不到多少(密度↑ 应 <1 且放大)
  X_real / X_engine  —— **净修正系数**,回测该乘的数

覆盖 eff1×b2_c26 的 0.945% 间距 / 0.210 穿越每bar,给出该点的定案读数。
用法: aggtrades_density.py
"""
import importlib.util
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd
import requests

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.sweep import GEARING, MAX_RATE
from gridtrade.core.grid_engine import grid_order_info

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_s = importlib.util.spec_from_file_location('ap', RD + '/aggtrades_path.py')
AP = importlib.util.module_from_spec(_s)
_s.loader.exec_module(AP)
_s2 = importlib.util.spec_from_file_location('av', RD + '/aggtrades_validate.py')
AV = importlib.util.module_from_spec(_s2)
_s2.loader.exec_module(AV)

TARGET_SPACING = [4.0, 3.0, 2.0, 1.5, 1.2, 0.945, 0.75, 0.5, 0.35]   # % ,含 b2_c26 的 0.945


def main():
    cl = AV.load_live()
    cache = ParquetCache(V.default_cache_root())
    sess = requests.Session()
    m1c, rows = {}, []
    done = 0
    for r in cl.itertuples(index=False):
        native = V.native_of(r.symbol)
        days = pd.date_range(r.t0.floor('D'), r.t1.floor('D'), freq='D')
        parts = [AP.fetch_agg(native, d.strftime('%Y-%m-%d'), sess) for d in days]
        parts = [p for p in parts if p is not None and len(p)]
        if len(parts) != len(days):
            continue
        ag = pd.concat(parts, ignore_index=True).sort_values('ts')
        t0ms, t1ms = int(r.t0.value // 10**6), int(r.t1.value // 10**6)
        ag = ag[(ag['ts'] >= t0ms) & (ag['ts'] <= t1ms)]
        if len(ag) < 50:
            continue
        if r.symbol not in m1c:
            m1c[r.symbol] = cache.read_all_days('1m', r.symbol)
        m1 = m1c[r.symbol]
        if m1 is None or m1.empty:
            continue
        bars = m1[(m1['candle_begin_time'] >= r.t0.floor('min'))
                  & (m1['candle_begin_time'] <= r.t1.ceil('min'))].reset_index(drop=True)
        if len(bars) < 10:
            continue
        true_px = ag['price'].to_numpy(float)
        true_ts = ag['ts'].to_numpy()
        tick_px = AP.tick_path_from_bars(bars)
        span = float(r.high_price) - float(r.low_price)
        entry = float(r.entry_price)
        done += 1
        for sp in TARGET_SPACING:
            n = int(round(span / (entry * sp / 100.0)))
            if n < 4 or n > 4000:
                continue
            gi = grid_order_info(float(r.cap or 1000.0), GEARING / MAX_RATE,
                                 float(r.low_price), float(r.high_price), n,
                                 float(r.low_price) * 0.99, float(r.high_price) * 1.01,
                                 0.0, MAX_RATE)
            if gi is None:
                continue
            lines = gi['价格序列']
            e_idx, _ = AP.crossings(tick_px, lines)
            t_idx, t_pos = AP.crossings(true_px, lines)
            ts = true_ts[t_pos] if len(t_pos) else t_pos
            x_fill, x_missed = AP.simulate_executor(t_idx, ts, lines, entry)
            rows.append({'symbol': r.symbol, 'target_sp': sp, 'n_lines': n,
                         'bars': len(bars), 'hold_h': (r.t1 - r.t0).total_seconds() / 3600,
                         'x_engine': len(e_idx), 'x_true': len(t_idx),
                         'x_real': x_fill, 'x_missed': x_missed})
        if done % 20 == 0:
            print('  ... %d 格' % done, flush=True)
    f = pd.DataFrame(rows)
    f.to_parquet(RD + '/ablation/aggtrades_density.parquet')
    print('\n对撞完成: %d 格 × %d 间距 = %d 组合\n' % (done, len(TARGET_SPACING), len(f)))
    g = f.groupby('target_sp').agg(n=('x_engine', 'size'),
                                   线数中位=('n_lines', 'median'),
                                   X_engine=('x_engine', 'sum'),
                                   X_true=('x_true', 'sum'),
                                   X_real=('x_real', 'sum'),
                                   错过=('x_missed', 'sum'),
                                   bars=('bars', 'sum'))
    g['密度/bar'] = g['X_engine'] / g['bars']
    g['真实/近似'] = g['X_true'] / g['X_engine']
    g['可实现/真实'] = g['X_real'] / g['X_true']
    g['净修正'] = g['X_real'] / g['X_engine']
    cols = ['n', '线数中位', '密度/bar', 'X_engine', 'X_true', 'X_real',
            '真实/近似', '可实现/真实', '净修正', '错过']
    print(g[cols].to_string(float_format=lambda x: '%.4f' % x))
    print('\n注:密度/bar 按 X_engine(回测口径)算,与闸门同源。')
    print('    实盘标定覆盖上界 = 0.0655/bar;eff1×b2_c26 = 0.210/bar。')


if __name__ == '__main__':
    main()
