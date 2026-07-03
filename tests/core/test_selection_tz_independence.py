import os
import time

import pandas as pd

from tests.golden.gen_golden import make_symbol_df


def _select():
    from gridtrade.core.selection import (proceed_calc_symbol_factor,
                                          select_grid_coin)
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    all_df = proceed_calc_symbol_factor(scd, run_time, '12H', 0)
    sel = select_grid_coin(all_df.copy(),
                           {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True},
                           [1, 1, 1], 2, run_time)
    return sel.sort_values(['time', 'symbol']).reset_index(drop=True)


def _run_under(tz):
    old = os.environ.get('TZ')
    os.environ['TZ'] = tz
    time.tzset()
    try:
        return _select()
    finally:
        if old is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old
        time.tzset()


def test_selection_independent_of_machine_tz():
    a = _run_under('UTC')
    b = _run_under('Asia/Shanghai')
    pd.testing.assert_frame_equal(a, b)
