"""选币回放（Live/Backtest parity + point-in-time）。复用 gridtrade.core.selection 的实盘选币纯函数。
构造每个 run_time 的 symbol_candle_data 时严格只用 (candle_begin_time + utc_offset) < run_time 的 bar、
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


def replay_selection(cache, symbols, run_times, strategy_config, factors,
                     utc_offset, on_select, *, timeframe='1h', log=print):
    period = strategy_config['period']
    weight_list = strategy_config['weight_list']
    choose_symbols = strategy_config['choose_symbols']
    max_candle_num = strategy_config['max_candle_num']
    if len(weight_list) != len(factors):
        log('[SR][WARN] weight_list(%d)!=factors(%d), 用等权' % (len(weight_list), len(factors)))
        weight_list = [1] * len(factors)

    series = load_full_series(cache, symbols, timeframe)
    processed = 0
    devnull = open(os.devnull, 'w')
    try:
        for run_time in run_times:
            run_time = pd.Timestamp(run_time)
            offset = compute_offset(run_time, period, utc_offset)
            symbol_candle_data = {}
            for s, df in series.items():
                mask = (df['candle_begin_time'] + pd.Timedelta(hours=utc_offset)) < run_time
                sub = df[mask]
                if len(sub) < 24:
                    continue
                symbol_candle_data[s] = sub.tail(max_candle_num).copy()
            if not symbol_candle_data:
                processed += 1
                continue
            with contextlib.redirect_stdout(devnull):
                all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset)
                if all_df is None or all_df.empty:
                    processed += 1
                    continue
                factor_data = select_grid_coin(all_df, factors, weight_list, choose_symbols, run_time)
            factor_data = factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= run_time]
            for _, row in factor_data.iterrows():
                on_select(run_time, offset, row)
            processed += 1
    finally:
        devnull.close()
    return processed
