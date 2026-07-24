"""按需算因子(回测提速):cal_factor(df, needed=set) 只算被引用的因子列,
留出因子逐位等于全算口径;proceed_calc + select_grid_coin 选中结果 diff==0(锚)。"""
import numpy as np
import pandas as pd

from tests.golden.gen_golden import make_symbol_df

# select_grid_coin 读的因子列 = config factors ∪ 硬编码过滤器
PROD_NEEDED = {'Reg_v2_5', 'Sgcz_5', 'Er_2', 'Reg_v2_2', 'Sgcz_2', 'db_volume_v1_2'}
# 回测 replay 还要多留布网几何读的 Atr_5/middle_5(下游 grid_params 消费)
REPLAY_NEEDED = PROD_NEEDED | {'Atr_5', 'middle_5'}


def test_cal_factor_needed_subset_matches_full():
    from gridtrade.core.factors import cal_factor
    df = make_symbol_df('BTC/USDT:USDT', n=240, seed=1)
    full = cal_factor(df.copy())
    pruned = cal_factor(df.copy(), needed=PROD_NEEDED)
    # 被请求的列逐位相等
    for col in PROD_NEEDED:
        np.testing.assert_allclose(
            pruned[col].to_numpy('float64'), full[col].to_numpy('float64'),
            rtol=0, atol=0, equal_nan=True, err_msg=f'needed factor {col} drifted')
    # 上涨/下跌/涨跌幅(截面因子输入)恒算
    for col in ('涨跌幅', '上涨', '下跌'):
        assert col in pruned.columns
    # 未被请求的贵列被跳过(证明确实省了活)
    for col in ('Reg_v2_3', 'Reg_v2_6', 'Er_5', 'Er_8', 'S_shape_5', 'ma_13'):
        assert col not in pruned.columns, f'{col} 不该被算'


def test_cal_factor_needed_none_is_full_baseline():
    """needed=None 完全等于现状(golden 基线不动)。"""
    from gridtrade.core.factors import cal_factor
    df = make_symbol_df('ETH/USDT:USDT', n=240, seed=3)
    a = cal_factor(df.copy())
    b = cal_factor(df.copy(), needed=None)
    assert list(a.columns) == list(b.columns)
    for col in a.columns:
        if a[col].dtype.kind in 'fi':
            np.testing.assert_array_equal(
                a[col].to_numpy(), b[col].to_numpy(), err_msg=f'{col} 漂移')


def test_needed_factors_covers_config_and_filter():
    from gridtrade.core.selection import needed_factors
    got = needed_factors({'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True})
    assert got == {'Reg_v2_5', 'Sgcz_5', 'Er_2', 'Reg_v2_2', 'Sgcz_2', 'db_volume_v1_2'}


def test_replay_passes_pruned_needed_to_batch(tmp_path, monkeypatch):
    """回测提速接线:replay 走向量化 batch 路,按 config+过滤器+几何裁出 needed 传给
    cal_factor_batch,而非全算(None)。"""
    import gridtrade.core.selection as SEL
    from tests.backtest.test_selection_replay import _seed_cache, STRAT, FACTORS
    from gridtrade.backtest.selection_replay import replay_selection
    seen = []
    real = SEL.cal_factor_batch

    def spy(df, needed=None):
        seen.append(needed)
        return real(df, needed=needed)

    monkeypatch.setattr(SEL, 'cal_factor_batch', spy)
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    replay_selection(cache, syms, [pd.Timestamp('2024-01-10 00:00:00')], STRAT, FACTORS,
                     lambda rt, off, row: None, timeframe='1h')
    assert seen, 'cal_factor_batch 未被调用'
    assert all(n == REPLAY_NEEDED for n in seen), f'期望裁剪集 {REPLAY_NEEDED},实得 {seen[:1]}'


def test_proceed_calc_pruned_selection_matches_full():
    """锚:全算 vs 按需算,选中币集合/顺序/rank_sum diff==0。"""
    from gridtrade.core.selection import proceed_calc_symbol_factor, select_grid_coin
    period, offset = '12H', 0
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    factors = {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True}
    wl = [1, 1, 1]
    full = select_grid_coin(
        proceed_calc_symbol_factor(scd, run_time, period, offset), factors, wl, 2, run_time)
    pruned = select_grid_coin(
        proceed_calc_symbol_factor(scd, run_time, period, offset, needed=PROD_NEEDED),
        factors, wl, 2, run_time)
    full = full.sort_values(['time', 'symbol']).reset_index(drop=True)
    pruned = pruned.sort_values(['time', 'symbol']).reset_index(drop=True)
    assert list(zip(full['symbol'], full['time'].astype(str))) == \
           list(zip(pruned['symbol'], pruned['time'].astype(str)))
    np.testing.assert_array_equal(full['rank_sum'].to_numpy(), pruned['rank_sum'].to_numpy())
    np.testing.assert_array_equal(full['close'].to_numpy(), pruned['close'].to_numpy())
