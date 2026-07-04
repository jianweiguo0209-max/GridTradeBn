"""选币回放（Live/Backtest parity + point-in-time）。复用 gridtrade.core.selection 的实盘选币纯函数。
构造每个 run_time 的 symbol_candle_data 时严格只用 candle_begin_time < run_time 的 bar（纯 UTC）、
取最近 max_candle_num 根，与实盘截断口径一致。
"""
import contextlib
import os
import time

import pandas as pd

from gridtrade.core.selection import (compute_offset, proceed_calc_symbol_factor,
                                      select_grid_coin)
from gridtrade.exchanges.base import CANDLE_COLS


def load_full_series(cache, symbols, timeframe='1h'):
    series = {}
    for s in symbols:
        df = cache.read_all_days(timeframe, s)
        if df is None or df.empty:
            continue
        df = df[CANDLE_COLS].copy()
        df.sort_values('candle_begin_time', inplace=True)
        df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)
        df.reset_index(drop=True, inplace=True)
        series[s] = df
    return series


def build_pit_candidates(series, run_time, *, max_candle_num,
                         min_quote_volume=0.0, blacklist=()):
    """逐 run_time 构造候选 K 线字典：PIT 截断(<run_time) + ≥24 根 + 绝对成交额地板 + 黑名单。
    绝对地板 = 前置 24 根 1h bar 的 quote_volume 之和（live dayNtlVlm 的缓存重建近似）。"""
    bl = set(blacklist)
    out = {}
    for s, df in series.items():
        if s in bl:                                   # 档0：无条件硬禁
            continue
        sub = df[df['candle_begin_time'] < run_time]  # PIT，无未来函数
        if len(sub) < 24:
            continue
        if min_quote_volume and min_quote_volume > 0:  # PIT 绝对成交额地板
            if float(sub.tail(24)['quote_volume'].sum()) < min_quote_volume:
                continue
        out[s] = sub.tail(max_candle_num).copy()
    return out


def _select_over_run_times(series, run_times, period, weight_list, factors,
                           choose_symbols, max_candle_num, min_quote_volume, blacklist):
    """逐 run_time 选币的纯循环体（串行/并行共用）。返回 [(run_time, offset, row)]。
    内部 redirect_stdout 抑制 core 选币函数的诊断 print（no data/[警告] 等）。"""
    out = []
    devnull = open(os.devnull, 'w')
    try:
        for run_time in run_times:
            run_time = pd.Timestamp(run_time)
            offset = compute_offset(run_time, period)
            symbol_candle_data = build_pit_candidates(
                series, run_time, max_candle_num=max_candle_num,
                min_quote_volume=min_quote_volume, blacklist=blacklist)
            if not symbol_candle_data:
                continue
            with contextlib.redirect_stdout(devnull):
                all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset)
                if all_df is None or all_df.empty:
                    continue
                factor_data = select_grid_coin(all_df, factors, weight_list, choose_symbols, run_time)
            factor_data = factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= run_time]
            for _, row in factor_data.iterrows():
                out.append((run_time, offset, row.copy()))
    finally:
        devnull.close()
    return out


def _split_contiguous(items, n):
    """把有序列表切成 n 段连续、近等长的子列表（保序；空段丢弃）。"""
    if not items:
        return []
    n = max(1, min(n, len(items)))
    k, m = divmod(len(items), n)
    out, i = [], 0
    for j in range(n):
        sz = k + (1 if j < m else 0)
        if sz:
            out.append(items[i:i + sz])
        i += sz
    return out


def _replay_chunk(payload):
    """进程池 worker（顶层、可 pickle）：各自从本地缓存载 series 后选自己那段 run_time。"""
    (cache, symbols, run_times_chunk, timeframe, period, weight_list, factors,
     choose_symbols, max_candle_num, min_quote_volume, blacklist) = payload
    series = load_full_series(cache, symbols, timeframe)
    return _select_over_run_times(series, run_times_chunk, period, weight_list, factors,
                                  choose_symbols, max_candle_num, min_quote_volume, blacklist)


def replay_selection(cache, symbols, run_times, strategy_config, factors, on_select, *,
                     timeframe='1h', min_quote_volume=0.0, blacklist=(), workers=1, log=print):
    period = strategy_config['period']
    weight_list = strategy_config['weight_list']
    choose_symbols = strategy_config['choose_symbols']
    max_candle_num = strategy_config['max_candle_num']
    if len(weight_list) != len(factors):
        log('[SR][WARN] weight_list(%d)!=factors(%d), 用等权' % (len(weight_list), len(factors)))
        weight_list = [1] * len(factors)

    run_times = list(run_times)
    if workers and workers > 1 and len(run_times) > 1:
        from concurrent.futures import ProcessPoolExecutor
        chunks = _split_contiguous(run_times, workers)
        payloads = [(cache, symbols, chunk, timeframe, period, weight_list, factors,
                     choose_symbols, max_candle_num, min_quote_volume, blacklist)
                    for chunk in chunks]
        with ProcessPoolExecutor(max_workers=len(payloads)) as ex:
            for chunk_result in ex.map(_replay_chunk, payloads):   # map 保输入序 ⇒ 与串行逐位一致
                for run_time, offset, row in chunk_result:
                    on_select(run_time, offset, row)
    else:
        series = load_full_series(cache, symbols, timeframe)
        for run_time, offset, row in _select_over_run_times(
                series, run_times, period, weight_list, factors,
                choose_symbols, max_candle_num, min_quote_volume, blacklist):
            on_select(run_time, offset, row)
    return len(run_times)
