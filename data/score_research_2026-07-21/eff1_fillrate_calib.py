"""maker 成交率曲线标定 fill_rate(spacing)(2026-07-26,**成交额口径** v2)。

**问题**:回测假设"价格穿越网格线 ⇒ 该线必成交"(maker 成交率 = 100%)。真实 maker 单需要
对手方吃到、且在队列前排;快速跳跃会跨过多档挂单而一笔不成交。若该假设在密格处失真,
回测收益会随间距变密而系统性虚高。

**口径(v2 关键修正)**:必须按**成交额/成交量**比,不能按**笔数**比。笔数两头都错:
  · 实盘 `grid_fills` 一行 = 一条 trade 记录 → 一个挂单被多个对手方分批吃 = 多行(**多算**,拆单比 1.65)
  · 按 (line_index, side) 去重 → 同一条线在持仓期内被反复穿越会被压成 1 次(**少算**)
  成交额是守恒量,上述两种误差都自动抵消。本脚本以「等效线数 = Σsize / order_num」
  与「成交额 = Σ(price×size)」双口径交叉验证。

**方法**(纯用已有数据,不需新增实盘运行):
  对每个实盘已关闭格,用**它自己的真实 grid 参数**(low/high/count)与**它自己的真实持仓时段**
  的 1m bar,跑引擎 touch→trade 链得「理论穿越数」,再折成同单位:
      fill_rate(量) = (Σ实盘size / order_num) / 理论穿越数
      fill_rate(额) = Σ实盘(price×size) / (Σ理论touch价 × order_num)
  按 spacing 分箱 → 得 fill_rate(spacing) 曲线。

**v2 第二处修正**:`drop_first_closest` 必须跟生产口径 = `neutral_init=False`
(见 backtest_run.py:145 与 grid_engine.get_trade_info docstring)。用默认 True 会吞掉
约七成格的首笔成交 → 理论端少算 → fill_rate 虚高。

用途:把方案 A(引擎加成交约束)从"拍脑袋参与率"变成实盘标定模型。
**本脚本只读**,不改引擎、不改任何结果。
用法: eff1_fillrate_calib.py
"""
import json
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.sweep import GEARING, MAX_RATE
from gridtrade.core.grid_engine import (get_trade_info, grid_order_info,
                                        grid_touch_info, trans_candle_to_tick)

LIVE = '/tmp/live_fills3.json'
OUT = ('/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
       '/ablation/fillrate_calib.parquet')


def main():
    raw = open(LIVE).read()
    raw = raw[raw.index('['):]
    d = pd.DataFrame(json.loads(raw))
    for c in ('low_price', 'high_price', 'entry_price', 'grid_count', 'order_num',
              'notional_live', 'qty_live', 'lines_hit', 'trades_raw',
              'cap', 'created_at', 'closed_at'):
        d[c] = pd.to_numeric(d[c], errors='coerce')
    cl = d[(d['status'] == 'CLOSED') & d['entry_price'].gt(0)
           & d['closed_at'].notna() & d['order_num'].gt(0)
           & d['qty_live'].gt(0)].copy()
    cl['t0'] = pd.to_datetime(cl['created_at'], unit='ms')
    cl['t1'] = pd.to_datetime(cl['closed_at'], unit='ms')
    cl['spacing_pct'] = ((cl['high_price'] - cl['low_price'])
                         / cl['grid_count'] / cl['entry_price'] * 100)
    cache = ParquetCache(V.default_cache_root())
    rows = []
    # 跳过原因分类:zero_theo = **实盘有真成交但引擎算出零穿越** = 首触/触网失效的直接证据,
    # 必须与"缺归档"分开计,否则会被埋掉(2026-07-26 教训)。
    skip = {'no_bars': 0, 'no_gi': 0, 'zero_theo': 0}
    zero_cases = []
    m1cache = {}
    for r in cl.itertuples(index=False):
        sym = r.symbol
        if sym not in m1cache:
            m1cache[sym] = cache.read_all_days('1m', sym)
        m1 = m1cache[sym]
        if m1 is None or m1.empty:
            skip['no_bars'] += 1
            continue
        bars = m1[(m1['candle_begin_time'] >= r.t0.floor('min'))
                  & (m1['candle_begin_time'] <= r.t1.ceil('min'))]
        bars = bars.reset_index(drop=True)
        if len(bars) < 10:
            skip['no_bars'] += 1
            continue
        gi = grid_order_info(float(r.cap or 1000.0), GEARING / MAX_RATE,
                             float(r.low_price), float(r.high_price),
                             int(r.grid_count),
                             float(r.low_price) * 0.99, float(r.high_price) * 1.01,
                             0.0, MAX_RATE)
        if gi is None:
            skip['no_gi'] += 1
            continue
        tick, _broke = trans_candle_to_tick(bars, gi)
        td = grid_touch_info(tick, gi)
        # 生产 = neutral_init False ⇒ 首触不丢弃(grid_engine docstring / backtest_run.py:145)
        tr = get_trade_info(td, float(r.entry_price), gi, drop_first_closest=False)
        if tr is None or len(tr) == 0:
            skip['zero_theo'] += 1
            zero_cases.append({'symbol': sym, 'spacing_pct': r.spacing_pct,
                               'hold_h': (r.t1 - r.t0).total_seconds() / 3600,
                               'live_qty_equiv': r.qty_live / r.order_num,
                               'live_notional': r.notional_live, 'bars': len(bars),
                               'touch_rows': 0 if td is None else len(td),
                               'lo': r.low_price, 'hi': r.high_price,
                               'entry': r.entry_price, 'n_lines': int(r.grid_count)})
            continue
        theo_x = len(tr)                                   # 理论穿越数(=回测口径 n_fills)
        theo_notional = float(tr['touch'].sum()) * r.order_num   # 折到实盘单线量
        rows.append({'symbol': sym, 'spacing_pct': r.spacing_pct,
                     'grid_count': int(r.grid_count),
                     'hold_h': (r.t1 - r.t0).total_seconds() / 3600,
                     'order_num': r.order_num, 'entry': r.entry_price,
                     'trades_raw': r.trades_raw, 'lines_hit': r.lines_hit,
                     'live_qty_equiv': r.qty_live / r.order_num,
                     'live_notional': r.notional_live,
                     'theo_x': theo_x, 'theo_notional': theo_notional})
    f = pd.DataFrame(rows)
    print('可标定格 n=%d  跳过: 缺bar=%d 建网失败=%d 引擎零穿越=%d'
          % (len(f), skip['no_bars'], skip['no_gi'], skip['zero_theo']), flush=True)
    if zero_cases:
        z = pd.DataFrame(zero_cases)
        print('\n!!!!! 引擎零穿越但实盘有真成交 —— 触网失效实证 %d 格 !!!!!' % len(z))
        print('  合计实盘等效线数=%.1f 成交额=%.0f USDT(引擎全判"未触网",pnl 恒 0)'
              % (z['live_qty_equiv'].sum(), z['live_notional'].sum()))
        print(z.to_string(float_format=lambda x: '%.4f' % x))
    else:
        print('  [首触/触网失效检查] 零穿越格 0 个 —— 无失效证据')
    f['rate_qty'] = f['live_qty_equiv'] / f['theo_x']
    f['rate_notional'] = f['live_notional'] / f['theo_notional']

    print('\n===== 总体(两口径交叉验证) =====')
    print('  【量】实盘等效线数 合计=%.1f  理论穿越 合计=%d  ⇒ fill_rate=%.3f'
          % (f['live_qty_equiv'].sum(), f['theo_x'].sum(),
             f['live_qty_equiv'].sum() / f['theo_x'].sum()))
    print('  【额】实盘成交额 合计=%.0f  理论成交额 合计=%.0f  ⇒ fill_rate=%.3f'
          % (f['live_notional'].sum(), f['theo_notional'].sum(),
             f['live_notional'].sum() / f['theo_notional'].sum()))
    print('  逐格 rate(额): median=%.3f mean=%.3f  q25=%.3f q75=%.3f'
          % (f['rate_notional'].median(), f['rate_notional'].mean(),
             f['rate_notional'].quantile(.25), f['rate_notional'].quantile(.75)))
    print('  对照旧笔数口径: trades_raw/theo=%.3f  lines_hit/theo=%.3f'
          % (f['trades_raw'].sum() / f['theo_x'].sum(),
             f['lines_hit'].sum() / f['theo_x'].sum()))
    print('  实盘每格: 等效线数 mean=%.2f median=%.2f max=%.1f'
          % (f['live_qty_equiv'].mean(), f['live_qty_equiv'].median(),
             f['live_qty_equiv'].max()))
    print('  回测每格: 理论穿越 mean=%.2f median=%.0f max=%d'
          % (f['theo_x'].mean(), f['theo_x'].median(), f['theo_x'].max()))

    print('\n===== fill_rate(spacing) 曲线(成交额口径) =====')
    bins = [0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 100]
    f['bin'] = pd.cut(f['spacing_pct'], bins)
    g = f.groupby('bin', observed=True).agg(
        n=('rate_notional', 'size'), spacing中位=('spacing_pct', 'median'),
        实盘等效线=('live_qty_equiv', 'sum'), 理论穿越=('theo_x', 'sum'),
        逐格rate中位=('rate_notional', 'median'))
    g['rate量'] = g['实盘等效线'] / g['理论穿越']
    g['rate额'] = (f.groupby('bin', observed=True)['live_notional'].sum()
                   / f.groupby('bin', observed=True)['theo_notional'].sum())
    print(g.to_string(float_format=lambda x: '%.3f' % x))

    sub = f[f['theo_x'] >= 2]
    if len(sub) > 8:
        rho = np.corrcoef(sub['spacing_pct'], sub['rate_notional'])[0, 1]
        print('\n  spacing↔rate(额) 相关(theo≥2, n=%d): pearson=%+.3f' % (len(sub), rho))
        med = sub['spacing_pct'].median()
        lo, hi = sub[sub['spacing_pct'] < med], sub[sub['spacing_pct'] >= med]
        print('  密半(<%.2f%%): rate额=%.3f (n=%d)   疏半: rate额=%.3f (n=%d)'
              % (med, lo['live_notional'].sum() / lo['theo_notional'].sum(), len(lo),
                 hi['live_notional'].sum() / hi['theo_notional'].sum(), len(hi)))
    print('\n  实盘 spacing 覆盖范围: min=%.3f%% q10=%.3f%% median=%.3f%% max=%.3f%%'
          % (f['spacing_pct'].min(), f['spacing_pct'].quantile(.1),
             f['spacing_pct'].median(), f['spacing_pct'].max()))
    # 真正被标定住的不是 spacing,而是「每 bar 穿越密度」:密度 <<1 时 4-tick 路径近似
    # 怎么画都不改计数;密度 >1 时计数**完全由该近似决定**,而它零验证。
    f['x_per_bar'] = f['theo_x'] / (f['hold_h'] * 60.0)
    print('  实盘标定覆盖的「穿越/bar」密度: median=%.4f q90=%.4f max=%.4f'
          % (f['x_per_bar'].median(), f['x_per_bar'].quantile(.9), f['x_per_bar'].max()))
    f.drop(columns=['bin'], errors='ignore').to_parquet(OUT)
    print('\n落盘 ablation/fillrate_calib.parquet')


if __name__ == '__main__':
    main()
