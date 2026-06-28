import os

import numpy as np
import pandas as pd

from tests.golden.gen_golden import make_symbol_df

_GOLDEN = os.path.join(os.path.dirname(__file__), '..', 'golden', 'factors_golden.parquet')
FACTOR_COLS = ['Reg_v2_2', 'Sgcz_2', 'Reg_v2_5', 'Sgcz_5', 'Er_2',
               'db_volume_v1_2', 'Atr_5', 'middle_5', 'ma_2', 'ma_5', 'ma_13', '涨跌幅']


def test_cal_factor_matches_golden():
    from gridtrade.core.factors import cal_factor
    df = make_symbol_df('BTC/USDT:USDT', n=240, seed=1)
    got = cal_factor(df.copy())
    golden = pd.read_parquet(_GOLDEN)
    for col in FACTOR_COLS:
        np.testing.assert_allclose(
            got[col].to_numpy(dtype='float64'),
            golden[col].to_numpy(dtype='float64'),
            rtol=1e-9, atol=1e-12, equal_nan=True, err_msg=f'factor {col} drifted',
        )
