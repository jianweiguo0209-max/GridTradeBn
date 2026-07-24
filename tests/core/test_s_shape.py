"""S_shape 波动形状因子(spike-dominance)单测:均匀波动≈1、单尖峰≫1、cal_factor 注册惰性列。"""
import numpy as np
import pandas as pd

from gridtrade.core.factors import S_shape_signal, cal_factor


def _bars(ranges, base=100.0):
    """按给定逐根振幅造 K 线:close 恒 base,high/low 对称摆动(TR=high-low)。"""
    n = len(ranges)
    return pd.DataFrame({
        'candle_begin_time': pd.date_range('2026-01-01', periods=n, freq='12H'),
        'time': pd.date_range('2026-01-01', periods=n, freq='12H'),
        'symbol': 'X/USDT:USDT',
        'open': base, 'close': base,
        'high': [base + r / 2 for r in ranges],
        'low': [base - r / 2 for r in ranges],
        'vol': 1.0, 'volCcy': 1.0, 'quote_volume': 1.0,
    })


def test_uniform_vol_is_one():
    df = _bars([2.0] * 8)
    S_shape_signal(df, 5, 0, 'S_shape_5')
    assert abs(df['S_shape_5'].iloc[-1] - 1.0) < 1e-6


def test_single_spike_dominates():
    # 近5根窗:4 根振幅 2 + 1 根振幅 20 → mean=5.6, median=2 → s=2.8
    df = _bars([2.0, 2.0, 2.0, 20.0, 2.0, 2.0])
    S_shape_signal(df, 5, 0, 'S_shape_5')
    s = df['S_shape_5'].iloc[-1]
    assert s > 1.3, s
    assert abs(s - 5.6 / 2.0) < 0.05


def test_cal_factor_registers_lazy_column():
    df = _bars([2.0] * 30)
    out = cal_factor(df)
    assert 'S_shape_5' in out.columns
    assert np.isfinite(out['S_shape_5'].iloc[-1])


def test_cal_factor_window_scan_columns_lazy():
    """因子窗扫描备选列(2026-07-21):cal_factor 产出全部窗变体列,末行有限(config 不引用=惰性)。"""
    df = _bars([2.0] * 30)
    out = cal_factor(df)
    for col in ('Reg_v2_3', 'Reg_v2_6', 'Sgcz_3', 'Sgcz_8', 'Er_3', 'Er_5', 'Er_8'):
        assert col in out.columns, col
        assert np.isfinite(out[col].iloc[-1]), col
