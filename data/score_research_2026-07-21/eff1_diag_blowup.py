"""密格几何 Calmar 爆炸 · 根因诊断(2026-07-26)。

现象:OOS 窗 eff1×geo_b2_c26 组合 ret +35932%(359倍)、Calmar 3.4e17,
而同窗同人群的 geo_b3_c16(现值)仅 ret +20.79 / C80.2。两者只差几何。

诊断链(逐层排除):
  ① 单格 pnl_ratio 分布 —— 是否存在离群格?量级是否物理可能?
  ② 网格参数 —— band/count/spacing 实际取值,间距是否低于 grid_spacing_min
  ③ 成交与费用 —— n_fills、单格费用占比,费率是否被正确扣除
  ④ 组合复利 —— metrics 的 lane 连乘是否被少数极端格主导
  ⑤ 杠杆/保证金 —— max_rate 是否真的约束住了名义敞口
用法: eff1_diag_blowup.py
"""
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
from gridtrade.core.grid_params import calc_grid_params_v2
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_spec = importlib.util.spec_from_file_location('sc', RD + '/eff1_scan.py')
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

WN = 'OOS'
S0, E0 = S.WD9[WN]
CASES = [('geo_b3_c16(现值)', {'band': 3, 'count_min': 16}),
         ('geo_b2_c26(爆炸)', {'band': 2, 'count_min': 26})]


def main():
    cache = ParquetCache(V.default_cache_root())
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    SW.set_baseline({})
    pool = pd.read_parquet(S.pool_path(WN))
    wd = S.preload(cache, S.make_picks(pool, 'K1', WN), WN, S0, E0, universe)
    print('[diag] %s wd 格=%d 币=%d days=%d\n' % (WN, len(wd.raw), wd.n_symbols, wd.days),
          flush=True)

    for label, geo in CASES:
        ov = dict(geo)
        df = SW.run_arm(wd, SW.Arm('eff1', label, ov), {}, workers=2)
        m = SW.metrics(df, wd.days)
        p = df['pnl_ratio']
        print('=' * 72)
        print('%s  组合 ret%+.2f%% MDD%.2f%% Calmar%.4g' %
              (label, m['ret'] * 100, m['mdd'] * 100, m['calmar']))
        print('① 单格 pnl_ratio 分布(%d 格):' % len(p))
        print('   mean%+.4f median%+.4f std%.4f min%+.4f max%+.4f'
              % (p.mean(), p.median(), p.std(), p.min(), p.max()))
        for q in (0.5, 0.9, 0.99, 0.999, 1.0):
            print('   q%-6s %+.4f' % (q, p.quantile(q)))
        print('   |pnl|>0.5 的格: %d   >1.0: %d   >2.0: %d'
              % ((p.abs() > 0.5).sum(), (p > 1.0).sum(), (p > 2.0).sum()))
        print('③ 成交 n_fills: mean%.1f median%.0f max%.0f'
              % (df['n_fills'].mean(), df['n_fills'].median(), df['n_fills'].max()))
        # ④ lane 连乘分解
        d = df.copy()
        d['close_ts'] = d['run_time'] + pd.to_timedelta(SW._S['period'])
        lane_end = {}
        for off, g in d.sort_values('close_ts').groupby('offset'):
            eq = (1.0 + g['pnl_ratio']).prod()
            lane_end[off] = (len(g), eq)
        print('④ 12 lane 各自末值(轮数, 连乘净值):')
        for off in sorted(lane_end):
            n, eq = lane_end[off]
            print('     off%-3d n=%-4d eq=%.4g' % (off, n, eq))
        top = d.nlargest(5, 'pnl_ratio')[['run_time', 'symbol', 'pnl_ratio',
                                          'n_fills', 'exit_reason']]
        print('② 最大 5 格:')
        for r in top.itertuples(index=False):
            print('     %s %-20s pnl%+.4f fills%-5.0f %s'
                  % (r.run_time, r.symbol, r.pnl_ratio, r.n_fills, r.exit_reason))
        # ② 网格参数实况(取最大格那笔)
        big = top.iloc[0]
        for rt, off, row, bars, fd, series in wd.raw:
            if rt == big.run_time and row['symbol'] == big.symbol:
                v2 = dict(SW._V2, atr_range_multiplier=geo['band'],
                          grid_count_min=geo['count_min'],
                          grid_spacing_max=SW.baseline()['spacing_max'],
                          stop_buffer_ratio=SW._V2['stop_buffer_ratio'])
                px = calc_grid_params_v2(row=row, price_limit=SW._S['price_limit'],
                                         stop_limit=SW._S['stop_limit'], v2_config=v2)
                lo_, hi_, gc = px['low_price'], px['high_price'], px['grid_count']
                sp = (hi_ - lo_) / gc / row['close']
                print('   ↳ 该格网格: close=%.6g low=%.6g high=%.6g 格数=%d 间距=%.4f%%'
                      % (row['close'], lo_, hi_, gc, sp * 100))
                print('     Atr_5=%.4f band=%s → 半宽=%.2f%%'
                      % (row['Atr_5'], geo['band'], (hi_ - lo_) / 2 / row['close'] * 100))
                break
        print()


if __name__ == '__main__':
    main()
