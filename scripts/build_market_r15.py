"""构建市场中位 15m 收益指数 market_r15.parquet(pv 截面条件化判别器的市场基准)。

语义:每分钟 t, mkt_r15(t) = 全归档票池(剔黑名单)各币 log(close_t/close_{t-15}) 的截面中位数;
截面样本 <30 币的分钟置 NaN(下游 merge 后 NaN→不紧,保守)。BTC 归档不全(仅 2026-06+),
截面中位本就更贴"全市场同动"语义。跨度=全部判定窗+留出窗 ±1 天(重跑幂等,存在即跳过)。
产物: <cache_root>/market_r15.parquet  列 candle_begin_time, mkt_r15(float32)
用法: .venv/bin/python scripts/build_market_r15.py [workers]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

SPANS = [('2024-09-28', '2024-12-03'),      # HOLD-B
         ('2025-01-30', '2025-04-02'),      # HOLD-A
         ('2025-08-13', '2025-12-16'),      # W1+W2
         ('2025-12-30', '2026-07-02')]      # OOS+IS
MIN_CROSS = 30


def _span_grid(s0, s1):
    return pd.date_range(pd.Timestamp(s0), pd.Timestamp(s1) + pd.Timedelta(days=1),
                         freq='1min')[:-1]


def _one(sym):
    """返回 [(span_idx, pos_int32, r15_float32), ...]:pos=该 span 分钟网格上的位置。"""
    cache = ParquetCache(V.default_cache_root())
    m = cache.read_all_days('1m', sym)
    if m is None or m.empty:
        return []
    m = m[['candle_begin_time', 'close']].sort_values('candle_begin_time')
    out = []
    for k, (s0, s1) in enumerate(SPANS):
        t0 = pd.Timestamp(s0)
        hi = pd.Timestamp(s1) + pd.Timedelta(days=1)
        seg = m[(m['candle_begin_time'] >= t0) & (m['candle_begin_time'] < hi)]
        if len(seg) < 500:
            continue
        # 日历对齐:log-close 落到分钟网格再做 15 分钟差——K线缺口(崩盘日断流)→NaN 传播,
        # 不会把跨缺口的位置位移当成 15m 收益(10-10 实测该 bug 使中位收益虚到 −0.82)
        n_grid = int((hi - t0) // pd.Timedelta(minutes=1))
        arr = np.full(n_grid, np.nan, dtype='float32')
        pos = ((seg['candle_begin_time'].values - t0.to_datetime64())
               // np.timedelta64(60, 's')).astype('int64')
        arr[pos] = np.log(seg['close'].astype(float).values)
        r15 = np.full(n_grid, np.nan, dtype='float32')
        r15[15:] = arr[15:] - arr[:-15]
        out.append((k, r15))
    return out


def main(workers=2):
    out_p = os.path.join(str(V.default_cache_root()), 'market_r15.parquet')
    if os.path.exists(out_p):
        print('SKIP(已有):', out_p, flush=True)
        return
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    print('universe=%d spans=%d workers=%d' % (len(universe), len(SPANS), workers), flush=True)
    grids = [_span_grid(s0, s1) for s0, s1 in SPANS]
    mats = [np.full((len(g), len(universe)), np.nan, dtype='float32') for g in grids]
    n_ok = 0
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, out in enumerate(ex.map(_one, universe, chunksize=4)):
            if out:
                n_ok += 1
                for k, r15 in out:
                    mats[k][:, i] = r15
            if (i + 1) % 100 == 0:
                print('  %d/%d 币, 有效 %d' % (i + 1, len(universe), n_ok), flush=True)
    parts = []
    for k, g in enumerate(grids):
        med = np.nanmedian(mats[k], axis=1).astype('float32')
        cnt = np.sum(~np.isnan(mats[k]), axis=1)
        med[cnt < MIN_CROSS] = np.nan
        parts.append(pd.DataFrame({'candle_begin_time': g, 'mkt_r15': med}))
        mats[k] = None
    df = pd.concat(parts, ignore_index=True).dropna(subset=['mkt_r15'])
    df = df.sort_values('candle_begin_time').reset_index(drop=True)
    df.to_parquet(out_p)
    print('DONE 行=%d 币=%d 覆盖=%s→%s → %s'
          % (len(df), n_ok, df['candle_begin_time'].min(),
             df['candle_begin_time'].max(), out_p), flush=True)


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2)
