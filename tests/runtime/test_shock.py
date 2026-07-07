# tests/runtime/test_shock.py
"""MarketShockBrake 信号纯函数(spec 2026-07-08-market-shock-brake):
PIT 截断、k 小时中位数、篮子不足 fail-open。"""
import pandas as pd

from gridtrade.runtime.shock import cross_median_k

RT = pd.Timestamp('2026-07-08 12:00')


def _candles(rets_4h, n_bars=8, leak_future=False):
    """构造 candles dict:每币 n_bars 根 1h 收盘,最后 4h 收益 = 指定值。"""
    out = {}
    for i, r in enumerate(rets_4h):
        idx = pd.date_range(RT - pd.Timedelta(hours=n_bars), periods=n_bars, freq='1H')
        step = min(4, n_bars)
        close = [100.0] * (n_bars - step) + [100.0 * (1 + r)] * step   # 阶跃在最后4根
        df = pd.DataFrame({'candle_begin_time': idx, 'close': close})
        if leak_future:   # 追加一根 rt 之后的暴涨 bar,PIT 必须剔掉
            df = pd.concat([df, pd.DataFrame({'candle_begin_time': [RT + pd.Timedelta(hours=1)],
                                              'close': [999.0]})], ignore_index=True)
        out['S%d/USDC:USDC' % i] = df
    return out


def test_median_of_cross_section():
    med = cross_median_k(_candles([-0.06, -0.05, -0.04, 0.0, 0.01]), RT, 4)
    assert abs(med - (-0.04)) < 1e-9                     # 中位数


def test_pit_excludes_future_bars():
    med = cross_median_k(_candles([-0.05] * 6, leak_future=True), RT, 4)
    assert abs(med - (-0.05)) < 1e-9                     # 未来 bar 不影响


def test_fail_open_when_basket_too_small():
    assert cross_median_k(_candles([-0.06] * 4), RT, 4) is None       # <5 币
    assert cross_median_k({}, RT, 4) is None
    assert cross_median_k(_candles([-0.06] * 6, n_bars=3), RT, 4) is None   # 根数不足 k+1
