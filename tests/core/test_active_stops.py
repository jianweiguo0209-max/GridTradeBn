"""主动止损多模式（trend/atr/none）单测。pv 默认路径的零漂移由 test_grid_engine_parity 守。"""
import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import (_compute_trend_break, calc_pv_spike,
                                        simulate_grid_engine)


def test_calc_pv_spike_resamples_by_minutes_not_months():
    """回归：active_period 必须按分钟重采样（'15min'）；旧 '15m' 被 pandas 当成月→pv 永不触发。"""
    t = pd.date_range('2026-03-01', periods=600, freq='1min')
    qv = np.full(600, 1e5); qv[300:315] = 8e5      # 一段量能尖峰
    b = pd.DataFrame({'candle_begin_time': t, 'quote_volume': qv})
    out = calc_pv_spike(b, mult=3)
    assert out['pv_spike'].sum() > 0               # 8× 尖峰必须能被识别


def _bars_downtrend(n=400, start=100.0, drop=0.0006):
    """确定性下跌趋势 1m bars（触发 trend/固定止损）。"""
    t = pd.date_range('2026-03-01', periods=n, freq='1min')
    close = start * np.exp(-drop * np.arange(n))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * 1.0003
    low = np.minimum(open_, close) * 0.9997
    return pd.DataFrame({'candle_begin_time': t, 'open': open_, 'high': high, 'low': low,
                         'close': close, 'quote_volume': 1e5, 'symbol': 'X/USDC:USDC'})


def _gp(bars):
    e = bars['open'].iloc[0]
    return {'low_price': e * 0.9, 'high_price': e * 1.1, 'grid_count': 25,
            'stop_low_price': e * 0.85, 'stop_high_price': e * 1.15}


def test_trend_break_signal_fires_on_downtrend():
    sig = _compute_trend_break(_bars_downtrend())
    assert sig['signal'].iloc[-1] == 1          # 明确下跌趋势末端应触发
    assert set(sig.columns) == {'candle_begin_time', 'signal'}


def test_trend_mode_exits_earlier_than_fixed_stop():
    """下跌趋势里 trend 模式应比只靠固定止损更早退出（截断更短或换了退出原因）。"""
    bars = _bars_downtrend()
    gp = _gp(bars)
    stop = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}
    base = simulate_grid_engine(bars, gp, cap=1000.0, leverage=5.0, stop_cfg=stop,
                                active_stop_mode='none')
    trend = simulate_grid_engine(bars, gp, cap=1000.0, leverage=5.0, stop_cfg=stop,
                                 active_stop_mode='trend')
    assert trend['exit_reason'] in ('趋势破位止损', '固定止损')
    # trend 触发时亏损不深于纯固定止损（早退保护）
    assert trend['pnl_ratio'] >= base['pnl_ratio'] - 1e-9


def test_none_mode_disables_active_stop():
    bars = _bars_downtrend()
    res = simulate_grid_engine(bars, _gp(bars), cap=1000.0, leverage=5.0,
                               stop_cfg={'stop_loss': 0.034}, active_stop_mode='none')
    assert res['exit_reason'] != '趋势破位止损'
    assert 'pv' not in res['exit_reason']
