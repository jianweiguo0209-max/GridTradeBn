"""一次性脚本：用原始 account_0 因子代码生成金标 fixture。
运行：TZ=Asia/Shanghai python tests/golden/gen_golden.py
依赖 talib。重构后由 parity 测试用相同输入比对新 core 输出。
"""
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
# 注入原始 account_0 到 sys.path（复刻 selection_replay 的做法）
for _p in (os.path.join(_ROOT, 'account_0'),
           os.path.join(_ROOT, 'account_0', 'utils'),
           os.path.join(_ROOT, 'account_0', 'api')):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def make_symbol_df(symbol, n=240, seed=0):
    """确定性合成 1H OHLCV（列与实盘 CANDLE_COLS 一致）。"""
    rng = np.random.RandomState(seed)
    rets = rng.normal(0, 0.01, size=n)
    close = 100.0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, size=n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, size=n)))
    vol = rng.uniform(1e3, 1e4, size=n)
    volccy = vol * close
    quote_volume = volccy * close
    t0 = pd.Timestamp('2024-01-01 00:00:00')
    cbt = pd.date_range(t0, periods=n, freq='1H')
    return pd.DataFrame({
        'symbol': symbol,
        'candle_begin_time': cbt,
        'open': open_, 'high': high, 'low': low, 'close': close,
        'vol': vol, 'volCcy': volccy, 'quote_volume': quote_volume,
    })


def main():
    from utils.fancy_grid_function import cal_factor  # 原始实现
    from utils.functions import (proceed_calc_symbol_factor,
                                 calc_grid_params_v1, calc_grid_params_v2)
    from utils.fancy_grid_function import select_grid_coin

    # ---- 1) 单币因子金标 ----
    df = make_symbol_df('BTC/USDT:USDT', n=240, seed=1)
    fac = cal_factor(df.copy())
    factor_cols = ['Reg_v2_2', 'Sgcz_2', 'Reg_v2_5', 'Sgcz_5', 'Er_2',
                   'db_volume_v1_2', 'Atr_5', 'middle_5', 'ma_2', 'ma_5', 'ma_13', '涨跌幅']
    fac[['candle_begin_time'] + factor_cols].to_parquet(
        os.path.join(_HERE, 'factors_golden.parquet'), index=False)

    # ---- 2) 截面因子 + 选币金标 ----
    period = '12H'
    offset = 0
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    all_df = proceed_calc_symbol_factor(scd, run_time, period, offset)
    factors = {"Reg_v2_5": True, "Sgcz_5": True, "Er_2": True}
    sel = select_grid_coin(all_df.copy(), factors, [1, 1, 1], 2, run_time)
    keep = ['symbol', 'time', 'rank', 'rank_sum', 'close', 'Atr_5', 'middle_5'] + list(factors.keys())
    sel = sel[[c for c in keep if c in sel.columns]].reset_index(drop=True)
    sel.to_parquet(os.path.join(_HERE, 'cross_select_golden.parquet'), index=False)

    # ---- 3) 网格参数金标（v1 + v2）----
    v2_config = {
        'atr_range_multiplier': 3, 'range_pct_min': 0.05, 'range_pct_max': 0.25,
        'grid_spacing_atr_ratio': 0.5, 'grid_spacing_min': 0.003, 'grid_spacing_max': 0.02,
        'grid_count_min': 25, 'grid_count_max': 149, 'stop_buffer_ratio': 0.01,
    }
    row = {'close': 123.45, 'Atr_5': 0.04, 'middle_5': 122.0}
    out = {
        'v1': calc_grid_params_v1(row, price_limit=[0.25, 0.25], stop_limit=0.01),
        'v2': calc_grid_params_v2(row, price_limit=[0.25, 0.25], stop_limit=0.01, v2_config=v2_config),
    }
    with open(os.path.join(_HERE, 'grid_params_golden.json'), 'w') as f:
        json.dump(out, f, indent=2)

    print('golden fixtures written to', _HERE)


if __name__ == '__main__':
    main()
