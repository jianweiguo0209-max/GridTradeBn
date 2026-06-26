"""
选币回放（对应 支柱一 Live/Backtest Parity + 支柱二 Point-in-Time）。

S1 候选发现：按小时游标回放实盘**完全相同**的选币函数，得到每个 run_time 选中的币。
不复制任何选币规则——直接 import account_0 的 proceed_calc_symbol_factor / select_grid_coin。

Point-in-time：构造每个 run_time 的 symbol_candle_data 时，严格只用
  (candle_begin_time + utc_offset) < run_time 的 1H bar，并取最近 max_candle_num 根，
这与实盘 api/kline.py:88 的截断逻辑一致。

时区警告：account_0 的选币函数内部读 time.localtime().tm_gmtoff。务必用与实盘服务器
一致的 TZ 运行本程序（本部署经 orderInfo.pkl 确认为 UTC+8，须 TZ=Asia/Shanghai），
否则 offset 与因子时间轴会漂移、parity 失效。
"""
import os
import sys
import time
import contextlib

import pandas as pd

from okx_history import CANDLE_COLS

# ---- 把 account_0 注入 sys.path，复用实盘选币管线 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ACCOUNT_DIR = os.path.join(os.path.dirname(_HERE), 'account_0')
for _p in (_ACCOUNT_DIR, os.path.join(_ACCOUNT_DIR, 'utils'), os.path.join(_ACCOUNT_DIR, 'api')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 实盘选币纯函数（parity 的核心）
from utils.functions import proceed_calc_symbol_factor  # noqa: E402
from utils.fancy_grid_function import select_grid_coin  # noqa: E402


def compute_offset(run_time, period, utc_offset):
    """复刻 functions.get_order_offset_tag 的 offset 计算。"""
    utc_run_time = run_time - pd.Timedelta(hours=utc_offset)
    return int(((utc_run_time - pd.to_datetime('2017-01-01')).total_seconds() / 3600) % int(period[:-1]))


def _load_full_series(cache, symbols):
    """把每个 symbol 的全量 1H 序列一次性读进内存（按天缓存合并）。"""
    series = {}
    for s in symbols:
        df = cache.read_all_days('1H', s)
        if df is None or df.empty:
            continue
        df = df[CANDLE_COLS].copy()
        df.sort_values('candle_begin_time', inplace=True)
        df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)
        df.reset_index(drop=True, inplace=True)
        series[s] = df
    return series


def replay_selection(cache, symbols, run_times, strategy_config, factors,
                     utc_offset, on_select, log=print):
    """
    对每个 run_time 回放选币，命中的币通过 on_select(run_time, offset, symbol, rank) 回调输出。
    返回处理过的 run_time 数。
    """
    period = strategy_config['period']
    weight_list = strategy_config['weight_list']
    choose_symbols = strategy_config['choose_symbols']
    max_candle_num = strategy_config['max_candle_num']

    log('[S1] 载入 %d 个币的全量 1H 序列到内存...' % len(symbols))
    t0 = time.time()
    series = _load_full_series(cache, symbols)
    log('[S1] 载入完成 %d 个有效币, 耗时 %.1fs' % (len(series), time.time() - t0))

    processed = 0
    devnull = open(os.devnull, 'w')
    try:
        for run_time in run_times:
            run_time = pd.Timestamp(run_time)
            offset = compute_offset(run_time, period, utc_offset)

            # 构造 point-in-time 的 symbol_candle_data（与实盘 fetch 截断口径一致）
            symbol_candle_data = {}
            cutoff = run_time
            for s, df in series.items():
                mask = (df['candle_begin_time'] + pd.Timedelta(hours=utc_offset)) < cutoff
                sub = df[mask]
                if len(sub) < 24:  # 与 proceed_calc_symbol_factor 的最小 bar 要求一致
                    continue
                symbol_candle_data[s] = sub.tail(max_candle_num).copy()

            if not symbol_candle_data:
                processed += 1
                continue

            # === 复用实盘函数：因子计算 + 选币（select_grid_coin 内部有大量 print，重定向静默）===
            with contextlib.redirect_stdout(devnull):
                all_data_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset)
                if all_data_df is None or all_data_df.empty:
                    processed += 1
                    continue
                factor_data = select_grid_coin(all_data_df, factors, weight_list, choose_symbols, run_time)

            # 只保留当前周期（与 proceed_order_for_strategy_config 一致）
            factor_data = factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= run_time]
            for _, row in factor_data.iterrows():
                # 回调传整行：含 symbol/rank + 布网所需 close/Atr_5/middle_5（供回测复用 calc_grid_params）
                on_select(run_time, offset, row)

            processed += 1
            if processed % 200 == 0:
                log('[S1] 已回放 %d/%d 个 run_time' % (processed, len(run_times)))
    finally:
        devnull.close()

    return processed
