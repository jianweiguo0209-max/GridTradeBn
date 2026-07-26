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

**IS 窗分段跑**(122 天 / 2735 格,整窗预热会撞爆 16GB 机器):
口径照 rsp2_is_split.py —— 两段各自只落**逐格明细**,merge 时合并明细后按**整窗 days**
调 SW.metrics ⇒ 与整窗跑逐位一致(metrics 只依赖逐格 run_time/offset/pnl_ratio)。
merge 阶段先用 b3_c16×off 对整窗存档自检,不过关就停手。

用法: [WN=OOS] tick_quantize_compare.py          # 整窗
      WN=IS SEG=1|2 tick_quantize_compare.py     # 跑一段
      WN=IS SEG=merge tick_quantize_compare.py   # 合并 + 自检 + 出表
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
SEG = os.environ.get('SEG', '')
ARMS = [(2, 26, 'b2_c26'), (2.5, 16, 'b2.5_c16'), (3, 16, 'b3_c16')]
MODES = ['off', 'stack', 'thin']
IS_SEGS = {'1': ('2026-03-01', '2026-04-30'), '2': ('2026-05-01', '2026-06-30')}
# merge 自检:整窗存档读数(eff1_scan_results.txt 的 P1/IS,off 模式即现状)
SELF_CHECK = ('b3_c16', 24.26, 3.19, 28.71)          # ret% / MDD% / Calmar


def seg_path(i):
    return '%s/ablation/tick_cmp_%s_seg%s.parquet' % (RD, WN, i)


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


def emit(r, days, tag):
    r.to_parquet('%s/ablation/tick_compare_%s.parquet' % (RD, tag))
    print('\n===== 汇总(%s) =====' % tag)
    print(r.to_string(index=False, float_format=lambda x: '%.4g' % x))
    print('\n===== 相对 off 的留存率 =====')
    for lab in [a[2] for a in ARMS]:
        b = r[(r['arm'] == lab) & (r['mode'] == 'off')]
        if b.empty:
            continue
        base = b['ret%'].iloc[0]
        line = []
        for mode in MODES[1:]:
            v = r[(r['arm'] == lab) & (r['mode'] == mode)]
            if v.empty:
                continue
            v = v['ret%'].iloc[0]
            line.append('%s: %+.2f%% (留存 %.1f%%)'
                        % (mode, v, v / base * 100 if base else float('nan')))
        print('  %-10s off %+.2f%%  →  %s' % (lab, base, '  |  '.join(line)))


def merge():
    parts = []
    for i in IS_SEGS:
        if not os.path.exists(seg_path(i)):
            print('缺 %s,先跑 SEG=%s' % (seg_path(i), i))
            return
        parts.append(pd.read_parquet(seg_path(i)))
    d = pd.concat(parts, ignore_index=True)
    full = T.S.WD9[WN]
    days = int((pd.Timestamp(full[1]) - pd.Timestamp(full[0])).days) + 1
    nm, r0, m0, c0 = SELF_CHECK
    sc = d[(d['_arm'] == nm) & (d['_mode'] == 'off')]
    m = SW.metrics(sc.drop(columns=['_arm', '_mode']), days)
    ok = (abs(m['ret'] * 100 - r0) <= 0.02 and abs(m['mdd'] * 100 - m0) <= 0.02
          and abs(m['calmar'] - c0) <= 0.15)
    print('[自检] %s×off 分段合并 ret%+.2f/MDD%.2f/C%.2f vs 整窗存档 ret%+.2f/MDD%.2f/C%.2f → %s'
          % (nm, m['ret'] * 100, m['mdd'] * 100, m['calmar'], r0, m0, c0,
             'PASS 口径等价' if ok else '**FAIL 停手,分段口径不等价**'), flush=True)
    if not ok:
        return
    rows = []
    for (lab, mode), g in d.groupby(['_arm', '_mode'], sort=False):
        mm = SW.metrics(g.drop(columns=['_arm', '_mode']), days)
        rows.append({'arm': lab, 'mode': mode, 'n_grids': len(g),
                     '建网失败': int((g['exit_reason'] == '建网失败').sum()),
                     'ret%': mm['ret'] * 100, 'MDD%': mm['mdd'] * 100,
                     'Calmar': mm['calmar'], 'fills': mm['n_fills']})
    emit(pd.DataFrame(rows), days, WN)


def main():
    if SEG == 'merge':
        return merge()
    SW.set_baseline({})
    s0, e0 = IS_SEGS[SEG] if SEG else T.S.WD9[WN]
    cache = ParquetCache(V.default_cache_root())
    wd = T.build_wd(cache, s0, e0)
    syms = sorted({row['symbol'] for _rt, _o, row in
                   [(a, b, c) for a, b, c, *_ in wd.raw]})
    print('[wd] %s 格=%d 币=%d 天=%d' % (WN, len(wd.raw), wd.n_symbols, wd.days), flush=True)
    tbs = daily_ticks(cache, syms, s0, e0)
    rows, details = [], []
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
            if SEG:
                d2 = df.copy()
                d2['_arm'], d2['_mode'] = lab, mode
                details.append(d2)
    if SEG:                      # 分段:只落逐格明细,指标留给 merge 按整窗 days 算
        pd.concat(details, ignore_index=True).to_parquet(seg_path(SEG))
        print('[seg%s] DONE 落盘 %s(段内指标仅供监看,正式读数以 merge 为准)'
              % (SEG, seg_path(SEG)))
        return
    emit(pd.DataFrame(rows), wd.days, WN)


if __name__ == '__main__':
    main()
