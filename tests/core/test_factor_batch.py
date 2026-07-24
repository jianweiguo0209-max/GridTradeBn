"""向量化按币分组算因子(回测提速 lever2):cal_factor_batch(堆叠多币帧) 与逐币 cal_factor
逐位一致(parity),proceed_calc_symbol_factor(batch=True) 选中结果与逐币 diff==0(锚)。"""
import numpy as np
import pandas as pd

from tests.golden.gen_golden import make_symbol_df
from gridtrade.core.factors import _ALL_FACTORS

_FACTOR_NAMES = [name for name, _ in _ALL_FACTORS]


def test_cal_factor_batch_matches_per_symbol_all_factors():
    from gridtrade.core.factors import cal_factor, cal_factor_batch
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    frames = {s: make_symbol_df(s, n=240, seed=i + 5) for i, s in enumerate(syms)}
    per = {s: cal_factor(f.copy()) for s, f in frames.items()}
    stacked = pd.concat([f.assign(symbol=s) for s, f in frames.items()], ignore_index=True)
    batch = cal_factor_batch(stacked)
    for s in syms:
        b = batch[batch['symbol'] == s].reset_index(drop=True)
        p = per[s].reset_index(drop=True)
        for col in _FACTOR_NAMES:
            np.testing.assert_allclose(
                b[col].to_numpy('float64'), p[col].to_numpy('float64'),
                rtol=1e-9, atol=1e-12, equal_nan=True, err_msg=f'{col} batch≠逐币')


def test_cal_factor_batch_needed_subset():
    from gridtrade.core.factors import cal_factor, cal_factor_batch
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    frames = {s: make_symbol_df(s, n=200, seed=i + 2) for i, s in enumerate(syms)}
    needed = {'Reg_v2_5', 'Sgcz_5', 'Er_2'}
    per = {s: cal_factor(f.copy(), needed=needed) for s, f in frames.items()}
    stacked = pd.concat([f.assign(symbol=s) for s, f in frames.items()], ignore_index=True)
    batch = cal_factor_batch(stacked, needed=needed)
    assert 'Atr_5' not in batch.columns   # 未请求不算
    for s in syms:
        b = batch[batch['symbol'] == s].reset_index(drop=True)
        p = per[s].reset_index(drop=True)
        for col in needed:
            np.testing.assert_allclose(b[col].to_numpy('float64'), p[col].to_numpy('float64'),
                                       rtol=1e-9, atol=1e-12, equal_nan=True, err_msg=col)


def test_proceed_calc_batch_selection_anchor():
    """锚:batch=True vs 逐币,选中币集合/顺序/rank_sum diff==0。"""
    from gridtrade.core.selection import proceed_calc_symbol_factor, select_grid_coin
    period, offset = '12H', 0
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    factors = {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True}
    wl = [1, 1, 1]

    def pick(batch):
        adf = proceed_calc_symbol_factor(scd, run_time, period, offset, batch=batch)
        return select_grid_coin(adf, factors, wl, 2, run_time).sort_values(
            ['time', 'symbol']).reset_index(drop=True)

    loop, vec = pick(False), pick(True)
    assert list(zip(loop['symbol'], loop['time'].astype(str))) == \
           list(zip(vec['symbol'], vec['time'].astype(str)))
    np.testing.assert_allclose(loop['rank_sum'].to_numpy(), vec['rank_sum'].to_numpy(),
                               rtol=1e-9, atol=1e-12)
