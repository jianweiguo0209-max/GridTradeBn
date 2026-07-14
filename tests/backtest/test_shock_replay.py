# tests/backtest/test_shock_replay.py
"""回测侧 Shock Brake 信号(spec 2026-07-08-market-shock-brake 回测同步):
向量化 median_signal/blocked_index + 与实盘 runtime.shock.cross_median_k 的逐 rt 同源守卫。"""
import numpy as np
import pandas as pd

from gridtrade.backtest.shock_replay import blocked_index, median_signal_series
from gridtrade.runtime.shock import cross_median_k


def _series(rets_4h, n_bars=48, start='2026-07-01', qv=2e6):
    """{sym: 1h df};最后 4 根收盘 = 100×(1+r) 阶跃。"""
    out = {}
    for i, r in enumerate(rets_4h):
        idx = pd.date_range(start, periods=n_bars, freq='1H')
        close = np.full(n_bars, 100.0)
        close[-4:] = 100.0 * (1 + r)
        out['S%d/USDC:USDC' % i] = pd.DataFrame(
            {'candle_begin_time': idx, 'open': close, 'high': close, 'low': close,
             'close': close, 'quote_volume': np.full(n_bars, float(qv))})
    return out


def test_parity_with_live_cross_median_k():
    """同源守卫:向量化 med 序列在每个评估时点与实盘逐 rt 计算逐位一致
    (同一篮子:全币过地板;实盘篮子在上游 floor 过滤,此处两路等价域)。"""
    series = _series([-0.06, -0.05, -0.03, 0.0, 0.02, 0.04])
    med = median_signal_series(series, k_hours=4, min_quote_volume=1e6)
    for rt in med.dropna().index[-6:]:
        live = cross_median_k(series, rt, 4)
        assert live is not None
        assert abs(med.loc[rt] - live) < 1e-12


def test_floor_masks_basket():
    # 3 个大跌币成交额不过地板 → 被剔出篮子,中位数由其余决定
    s = _series([-0.09, -0.09, -0.09, 0.0, 0.0, 0.01])
    for i in range(3):
        s['S%d/USDC:USDC' % i]['quote_volume'] = 10.0    # 地板下
    med = median_signal_series(s, k_hours=4, min_quote_volume=1e6)
    assert abs(med.dropna().iloc[-1] - 0.0) < 1e-12


def test_blocked_index_window_semantics():
    """fired at t → 封锁 [t, t+X)(小时粒度)。"""
    s = _series([-0.06] * 6)
    med = median_signal_series(s, k_hours=4, min_quote_volume=0)
    # 阶跃在最后 4 根 → 最后 4 个评估时点 fired;X=2 → 封锁窗覆盖 fired∪其后1h
    rts = pd.date_range(med.index[0], med.index[-1] + pd.Timedelta(hours=3), freq='1H')
    blk = blocked_index(med, thr=0.04, x_hours=2, rts=rts)
    fired_first = med.index[med.abs() >= 0.04][0]
    assert bool(blk.loc[fired_first]) and bool(blk.loc[fired_first + pd.Timedelta(hours=1)])
    assert not bool(blk.loc[fired_first - pd.Timedelta(hours=1)])
    last_fired = med.index[med.abs() >= 0.04][-1]
    assert bool(blk.loc[last_fired + pd.Timedelta(hours=1)])       # X=2 拖尾 1h
    assert not bool(blk.loc[last_fired + pd.Timedelta(hours=2)])   # 过窗恢复


def test_top_pct_masks_basket():
    # 相对口径篮子：pct=0.5 两币取 ceil(1)=1 → 只剩高量币，med=其收益（与票池口径同步，
    # spec 2026-07-14-universe-top-volume-pct）
    from gridtrade.backtest.shock_replay import median_signal_series
    idx = pd.date_range('2024-01-01', periods=40, freq='1H')
    hi = pd.DataFrame({'candle_begin_time': idx,
                       'close': [100.0] * 30 + [110.0] * 10,
                       'quote_volume': 1000.0})
    lo = pd.DataFrame({'candle_begin_time': idx,
                       'close': 100.0, 'quote_volume': 10.0})
    series = {'H/USDT:USDT': hi, 'L/USDT:USDT': lo}
    t = pd.Timestamp('2024-01-02 07:00:00')   # bar30(110) 收盘后评估点，k=4 → ret=10%
    med_all = median_signal_series(series, 4)
    med_top = median_signal_series(series, 4, top_volume_pct=0.5)
    assert abs(med_all[t] - 0.05) < 1e-9      # 全篮中位([10%, 0]) = 5%
    assert abs(med_top[t] - 0.10) < 1e-9      # top50% 只剩 H → 10%
