"""tick 量化对照:同一批格、同一段行情,唯一变量 = 网格线是否量化到交易所 tickSize。

**要回答的问题**:eff1 密格臂的收益,是真实的网格套利,还是在吃引擎的价格分辨率盲区?
(引擎原把价格当连续量,网格线可比 tickSize 还密 ⇒ 实盘挂不出来的线也被记了穿越;
 实测 b2_c26×OOS 有 **98.6% 的 Σpnl 落在间距<2tick 区**,见 tick_resolution_audit.py)

**三个模式**(见 grid_engine.grid_order_info):
  off    price_tick=0,现状(对照锚,必须逐位复现历史读数)
  stack  量化+合并同价线,order_num 按合并后重算 ⇒ **总名义不变**,贴近实盘
         (executor 仍逐线挂 N 张单,坍缩处同价位堆 k×order_num)
  thin   量化+合并,每线量固定为未量化值 ⇒ 总名义变小,作**下界**

**tick 来源**:逐 (币,日) 从 1m 归档推(当日全部不同 OHLC 价格的最小正差)。
权威 `exchangeInfo` 只有**当前**快照,拿不到历史;推断法在 2026-07 窗对 9 个币
**9/9 命中权威值**,且能正确捕捉 FLOW 2026-01-28 的 0.001→0.00001 变更 ⇒ 可信。
⚠ 必须逐日而非逐窗常数:tickSize 会被交易所调整。

用法: [WN=OOS] tick_quantize_compare.py
"""
import importlib.util
import os
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_s = importlib.util.spec_from_file_location('tt', RD + '/b2c26_trim_top.py')
T = importlib.util.module_from_spec(_s)
_s.loader.exec_module(T)

WN = os.environ.get('WN', 'OOS')
ARMS = [(2, 26, 'b2_c26'), (2.5, 16, 'b2.5_c16'), (3, 16, 'b3_c16')]
MODES = ['off', 'stack', 'thin']


def daily_ticks(cache, syms, s0, e0):
    """{symbol: {'YYYY-MM-DD': tick}} —— 逐日推断(tickSize 会被交易所调整)。"""
    lo = str(pd.Timestamp(s0).date())
    hi = str((pd.Timestamp(e0) + pd.Timedelta(days=2)).date())
    out, n_chg = {}, 0
    for s in syms:
        m = cache.read_days_range('1m', s, lo, hi)
        if m is None or m.empty:
            continue
        by = {}
        for day, g in m.groupby(m['candle_begin_time'].dt.date):
            px = np.unique(np.concatenate([g['open'], g['high'], g['low'], g['close']]))
            dd = np.diff(px)
            dd = dd[dd > 1e-12]
            if len(dd):
                by[str(day)] = float(dd.min())
        if by:
            out[s] = by
            if len(set(by.values())) > 1:
                n_chg += 1
    print('  tick 推断: %d 币,其中 %d 个窗内发生过变更' % (len(out), n_chg), flush=True)
    return out


def main():
    SW.set_baseline({})
    s0, e0 = T.S.WD9[WN]
    cache = ParquetCache(V.default_cache_root())
    wd = T.build_wd(cache, s0, e0)
    syms = sorted({row['symbol'] for _rt, _o, row in
                   [(a, b, c) for a, b, c, *_ in wd.raw]})
    print('[wd] %s 格=%d 币=%d 天=%d' % (WN, len(wd.raw), wd.n_symbols, wd.days), flush=True)
    tbs = daily_ticks(cache, syms, s0, e0)
    rows = []
    for band, cnt, lab in ARMS:
        for mode in MODES:
            kw = {} if mode == 'off' else {'tick_by_sym': tbs, 'tick_mode': mode}
            df = SW.run_arm(wd, SW.Arm('eff1', 'geo_' + lab,
                                       {'band': band, 'count_min': cnt}), {},
                            workers=2, **kw)
            m = SW.metrics(df, wd.days)
            nfail = int((df['exit_reason'] == '建网失败').sum()) if len(df) else 0
            rows.append({'arm': lab, 'mode': mode, 'n_grids': len(df),
                         '建网失败': nfail, 'ret%': m['ret'] * 100,
                         'MDD%': m['mdd'] * 100, 'Calmar': m['calmar'],
                         'fills': m['n_fills']})
            print('  %-10s %-6s 格=%-5d 失败=%-4d ret%+12.2f%% MDD%5.2f%% C=%-11.4g fills=%.1f'
                  % (lab, mode, len(df), nfail, m['ret'] * 100, m['mdd'] * 100,
                     m['calmar'], m['n_fills']), flush=True)
    r = pd.DataFrame(rows)
    r.to_parquet('%s/ablation/tick_compare_%s.parquet' % (RD, WN))
    print('\n===== 汇总(%s) =====' % WN)
    print(r.to_string(index=False, float_format=lambda x: '%.4g' % x))
    print('\n===== 相对 off 的留存率 =====')
    for lab in [a[2] for a in ARMS]:
        base = r[(r['arm'] == lab) & (r['mode'] == 'off')]['ret%'].iloc[0]
        line = []
        for mode in MODES[1:]:
            v = r[(r['arm'] == lab) & (r['mode'] == mode)]['ret%'].iloc[0]
            line.append('%s: %+.2f%% (留存 %.1f%%)' % (mode, v, v / base * 100 if base else float('nan')))
        print('  %-10s off %+.2f%%  →  %s' % (lab, base, '  |  '.join(line)))


if __name__ == '__main__':
    main()
