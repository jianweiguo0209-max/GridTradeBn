import os

import numpy as np
import pandas as pd

from tests.golden.gen_golden import make_symbol_df

_GOLDEN = os.path.join(os.path.dirname(__file__), '..', 'golden', 'cross_select_golden.parquet')


def _run_new():
    from gridtrade.core.selection import proceed_calc_symbol_factor, select_grid_coin
    period, offset = '12H', 0
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    all_df = proceed_calc_symbol_factor(scd, run_time, period, offset)
    factors = {"Reg_v2_5": True, "Sgcz_5": True, "Er_2": True}
    return select_grid_coin(all_df.copy(), factors, [1, 1, 1], 2, run_time)


def _norm(df):
    df = df.copy()
    df['time'] = pd.to_datetime(df['time'])
    return df.sort_values(['time', 'symbol']).reset_index(drop=True)


def test_selection_matches_golden():
    got = _norm(_run_new())
    golden = _norm(pd.read_parquet(_GOLDEN))
    # 同样的 (time, symbol) 选中集合与顺序
    assert list(zip(got['symbol'], got['time'].astype(str))) == \
           list(zip(golden['symbol'], golden['time'].astype(str)))
    for col in ['rank', 'rank_sum', 'close', 'Atr_5', 'middle_5']:
        np.testing.assert_allclose(
            got[col].to_numpy('float64'), golden[col].to_numpy('float64'),
            rtol=1e-9, atol=1e-12, equal_nan=True, err_msg=f'{col} drifted')


def test_compute_offset_is_pure_utc():
    from gridtrade.core.selection import compute_offset
    run_time = pd.Timestamp('2024-01-09 05:00:00')
    # 纯 UTC：utc_run_time == run_time（不再 −8）
    expected = int(((run_time - pd.to_datetime('2017-01-01')).total_seconds() / 3600) % 12)
    assert compute_offset(run_time, '12H') == expected
