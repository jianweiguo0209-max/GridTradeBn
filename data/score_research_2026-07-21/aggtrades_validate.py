"""第一步:在**已知点**校准仪器 —— 用实盘 131 格的真实 aggTrades 路径,验证
X_realizable(模拟 executor)≈ 实盘真实成交额口径成交数。

只有这一步过了,第二步"把间距人工收紧、沿密度轴外推"才可信。
三数同时出:X_engine(4-tick 近似)/ X_true(真实路径穿越)/ X_realizable(可实现成交)。
用法: aggtrades_validate.py
"""
import importlib.util
import json
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd
import requests

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.sweep import GEARING, MAX_RATE
from gridtrade.core.grid_engine import (get_trade_info, grid_order_info,
                                        grid_touch_info, trans_candle_to_tick)

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_s = importlib.util.spec_from_file_location('ap', RD + '/aggtrades_path.py')
AP = importlib.util.module_from_spec(_s)
_s.loader.exec_module(AP)


def load_live():
    raw = open('/tmp/live_fills3.json').read()
    d = pd.DataFrame(json.loads(raw[raw.index('['):]))
    for c in ('low_price', 'high_price', 'entry_price', 'grid_count', 'order_num',
              'qty_live', 'notional_live', 'cap', 'created_at', 'closed_at'):
        d[c] = pd.to_numeric(d[c], errors='coerce')
    d = d[(d['status'] == 'CLOSED') & d['entry_price'].gt(0) & d['closed_at'].notna()
          & d['order_num'].gt(0) & d['qty_live'].gt(0)].copy()
    d['t0'] = pd.to_datetime(d['created_at'], unit='ms')
    d['t1'] = pd.to_datetime(d['closed_at'], unit='ms')
    return d


def main():
    cl = load_live()
    cache = ParquetCache(V.default_cache_root())
    sess = requests.Session()
    m1c, rows, miss = {}, [], 0
    for r in cl.itertuples(index=False):
        native = V.native_of(r.symbol)
        days = pd.date_range(r.t0.floor('D'), r.t1.floor('D'), freq='D')
        parts = [AP.fetch_agg(native, d.strftime('%Y-%m-%d'), sess) for d in days]
        parts = [p for p in parts if p is not None and len(p)]
        if len(parts) != len(days):
            miss += 1
            continue
        ag = pd.concat(parts, ignore_index=True).sort_values('ts')
        t0ms, t1ms = int(r.t0.value // 10**6), int(r.t1.value // 10**6)
        ag = ag[(ag['ts'] >= t0ms) & (ag['ts'] <= t1ms)]
        if len(ag) < 50:
            miss += 1
            continue
        gi = grid_order_info(float(r.cap or 1000.0), GEARING / MAX_RATE,
                             float(r.low_price), float(r.high_price), int(r.grid_count),
                             float(r.low_price) * 0.99, float(r.high_price) * 1.01,
                             0.0, MAX_RATE)
        if gi is None:
            miss += 1
            continue
        lines = gi['价格序列']
        # X_engine: 走引擎自己的 1m→4tick 链(与回测逐位同源)
        if r.symbol not in m1c:
            m1c[r.symbol] = cache.read_all_days('1m', r.symbol)
        m1 = m1c[r.symbol]
        x_eng = None
        if m1 is not None and not m1.empty:
            bars = m1[(m1['candle_begin_time'] >= r.t0.floor('min'))
                      & (m1['candle_begin_time'] <= r.t1.ceil('min'))].reset_index(drop=True)
            if len(bars) >= 10:
                tick, _b = trans_candle_to_tick(bars, gi)
                tr = get_trade_info(grid_touch_info(tick, gi), float(r.entry_price),
                                    gi, drop_first_closest=False)
                x_eng = 0 if tr is None else len(tr)
        # X_true / X_realizable: 真实 aggTrades 路径
        idx, pos = AP.crossings(ag['price'].to_numpy(float), lines)
        ts = ag['ts'].to_numpy()[pos] if len(pos) else pos
        x_fill, x_missed = AP.simulate_executor(idx, ts, lines, float(r.entry_price))
        rows.append({'symbol': r.symbol, 'n_lines': int(r.grid_count),
                     'spacing_pct': (r.high_price - r.low_price) / r.grid_count
                     / r.entry_price * 100,
                     'hold_h': (r.t1 - r.t0).total_seconds() / 3600,
                     'n_trades': len(ag),
                     'live': r.qty_live / r.order_num,
                     'x_engine': x_eng, 'x_true': len(idx),
                     'x_real': x_fill, 'x_missed': x_missed})
        if len(rows) % 20 == 0:
            print('  ... %d 格' % len(rows), flush=True)
    f = pd.DataFrame(rows).dropna(subset=['x_engine'])
    f.to_parquet(RD + '/ablation/aggtrades_validate.parquet')
    print('\n可对撞格 n=%d (缺 aggTrades/数据不足 %d)' % (len(f), miss))
    print('\n===== 三数对撞(合计) =====')
    for c, lab in (('x_engine', '回测 4-tick 近似'), ('x_true', '真实路径穿越'),
                   ('x_real', '真实路径+executor 可实现'), ('live', '实盘真值(成交额口径)')):
        print('  %-26s 合计=%8.1f  每格 mean=%.2f' % (lab, f[c].sum(), f[c].mean()))
    print('\n  X_true / X_engine      = %.4f   ← 4-tick 近似的失真' % (f['x_true'].sum() / f['x_engine'].sum()))
    print('  X_real / X_true        = %.4f   ← 5s 补单延迟的损耗' % (f['x_real'].sum() / f['x_true'].sum()))
    print('  X_real / X_engine      = %.4f   ← **净修正系数**' % (f['x_real'].sum() / f['x_engine'].sum()))
    print('  X_real / 实盘真值      = %.4f   ← **仪器校准:应 ≈ 1**' % (f['x_real'].sum() / f['live'].sum()))
    print('  X_engine / 实盘真值    = %.4f   (前次成交额标定得 1.004 的倒数向)'
          % (f['x_engine'].sum() / f['live'].sum()))
    print('\n  逐格 |X_real - live| == 0 的比例: %.1f%%'
          % ((f['x_real'] - f['live']).abs().lt(0.5).mean() * 100))
    print('  错过(缺挂单)穿越 合计=%d' % f['x_missed'].sum())


if __name__ == '__main__':
    main()
