"""p12 选币器注入·可行性探测(2026-07-25,轻活:2轮选币,不动重仗)。

验三件事(全部是后续 p12_final_bt.py 的地基假设):
  ① row 字段:布网 calc_grid_params_v2 需要的列在候选行里齐不齐
  ② 全池截断恒等:choose_symbols=BIG 后按 rank<=5 截断,是否与 choose_symbols=5 逐位一致
     ——若是,则"一次全池回放服务所有臂"成立,且**锚臂可证 byte-exact**
  ③ 全池规模:每轮候选数(决定 p12 top-1 的真实选择面,须与探针 in_pool 口径同量级)
用法: .venv/bin/python data/score_research_2026-07-21/p12_inject_probe.py
"""
import sys

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import BT_FACTORS, BT_STRATEGY, BT_UNIVERSE_TOP_PCT
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import _select_over_run_times, load_full_series
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

# 用已有档案的 HOLD-A 窗探测(不碰新留出窗,避免任何"看过留出"的嫌疑)
RTS = [pd.Timestamp('2025-02-10 08:00'), pd.Timestamp('2025-02-10 09:00')]


def _run(series, rts, choose_symbols):
    s = dict(BT_STRATEGY, choose_symbols=choose_symbols)
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    return _select_over_run_times(
        series, rts, s['period'], s['weight_list'], BT_FACTORS,
        s['choose_symbols'], s['max_candle_num'], 0.0, bl,
        top_volume_pct=BT_UNIVERSE_TOP_PCT)


def main():
    cache = ParquetCache(V.default_cache_root())
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    syms = sorted(set(V.list_archive_symbols()) - set(bl))
    print('[probe] universe=%d 载 1h...' % len(syms), flush=True)
    series = load_full_series(cache, syms, '1h')
    lo = RTS[0] - pd.Timedelta(days=10)
    hi = RTS[-1] + pd.Timedelta(days=2)
    for s_ in list(series):                       # 裁窗省内存(同 cf_run)
        df = series[s_]
        df = df[(df['candle_begin_time'] >= lo) & (df['candle_begin_time'] < hi)]
        if len(df) < 24:
            del series[s_]
        else:
            series[s_] = df.reset_index(drop=True)
    print('[probe] 有效币=%d' % len(series), flush=True)

    k5 = _run(series, RTS, 5)
    big = _run(series, RTS, 9999)
    print('\n① row 字段(%d 列):' % len(k5[0][2].index), flush=True)
    print('   ', list(k5[0][2].index), flush=True)
    need = ['symbol', 'rank', 'close', 'Atr_5', 'middle_5', 'time']
    print('   布网/递补必需列在不在:',
          {c: (c in k5[0][2].index) for c in need}, flush=True)

    print('\n③ 全池规模:', flush=True)
    for rt in RTS:
        n = sum(1 for r in big if r[0] == rt)
        print('   %s 候选=%d' % (rt, n), flush=True)

    print('\n② 全池截断 vs K=5 逐位比对:', flush=True)
    ok = True
    for rt in RTS:
        a = [r for r in k5 if r[0] == rt]
        b = [r for r in big if r[0] == rt and float(r[2]['rank']) <= 5]
        b = sorted(b, key=lambda t: float(t[2]['rank']))
        a = sorted(a, key=lambda t: float(t[2]['rank']))
        same_sym = [x[2]['symbol'] for x in a] == [x[2]['symbol'] for x in b]
        same_rank = [float(x[2]['rank']) for x in a] == [float(x[2]['rank']) for x in b]
        # 逐列全值比对(布网输入必须逐位同)
        cols_same = True
        for x, y in zip(a, b):
            xs, ys = x[2], y[2]
            for c in xs.index:
                if c in ys.index:
                    u, v = xs[c], ys[c]
                    if isinstance(u, float) and pd.isna(u) and pd.isna(v):
                        continue
                    if u != v:
                        cols_same = False
                        print('     差异 %s.%s: %r vs %r' % (xs['symbol'], c, u, v), flush=True)
        print('   %s: n=%d/%d symbol同=%s rank同=%s 全列同=%s'
              % (rt, len(a), len(b), same_sym, same_rank, cols_same), flush=True)
        ok = ok and same_sym and same_rank and cols_same and len(a) == len(b)
    print('\n结论 ②全池截断恒等: %s' % ('PASS(可一次回放服务所有臂)' if ok else 'FAIL(须分臂回放)'),
          flush=True)


if __name__ == '__main__':
    main()
