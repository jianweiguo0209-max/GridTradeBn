import warnings

import numpy as np
import pandas as pd


def test_resample_base_offset_equivalent():
    """证明 base=k（小时）≡ offset=Timedelta(hours=k)，全相位 0..11（base→offset 迁移的安全性依据）。"""
    t = pd.date_range('2024-01-01 00:00:00', periods=240, freq='1H')
    df = pd.DataFrame({'x': np.arange(240, dtype='float64')}, index=t)
    for k in range(12):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', FutureWarning)   # 老写法刻意触发，仅作对比基准
            old = df.resample('12H', base=k).sum()
        new = df.resample('12H', offset=pd.Timedelta(hours=k)).sum()
        pd.testing.assert_frame_equal(old, new)


def test_selection_path_emits_no_target_warnings():
    """选币路径（含 offset≠0）不得再冒 base= FutureWarning / SettingWithCopyWarning。"""
    from tests.golden.gen_golden import make_symbol_df
    from gridtrade.core.selection import proceed_calc_symbol_factor, select_grid_coin
    period, offset = '12H', 3                              # 非零相位，走新 offset= 路径
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter('always')                   # 本地覆盖 pyproject 的全局 ignore
        all_df = proceed_calc_symbol_factor(scd, run_time, period, offset)
        select_grid_coin(all_df.copy(), {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True},
                         [1, 1, 1], 2, run_time)
    msgs = [str(w.message) for w in rec]
    names = [type(w.message).__name__ for w in rec]
    assert not any("'base' in" in m for m in msgs), msgs
    assert 'SettingWithCopyWarning' not in names, names
