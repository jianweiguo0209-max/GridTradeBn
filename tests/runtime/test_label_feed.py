import numpy as np
import pandas as pd

from gridtrade.runtime.label_feed import LabelFeed


class FakeAdapter:
    """合成 1m 行情:恒价 100,仅 GOOD/USDT 在窗内某分钟拉高 8%。记录取数区间。"""
    def __init__(self):
        self.calls = []

    def fetch_ohlcv(self, sym, tf, start_ms, end_ms):
        assert tf == '1m'
        self.calls.append((sym, start_ms, end_ms))
        t = pd.date_range(pd.Timestamp(start_ms, unit='ms'),
                          pd.Timestamp(end_ms, unit='ms'), freq='1min')
        t = t[t <= pd.Timestamp(end_ms, unit='ms')]
        df = pd.DataFrame({'ts': (t.asi8 // 10**6),
                           'candle_begin_time': t, 'symbol': sym,
                           'open': 100.0, 'high': 100.0, 'low': 100.0,
                           'close': 100.0, 'vol': 1.0})
        if sym == 'GOOD/USDT' and len(df) > 400:
            df.loc[400, 'high'] = 108.0
        return df


def test_cold_then_incremental_and_labels():
    ad = FakeAdapter()
    feed = LabelFeed(ad, pace_ms=0, cold_pace_ms=0, sleep=lambda s: None)
    rt = pd.Timestamp('2026-07-27 03:00:00')
    feed.update(['GOOD/USDT', 'FLAT/USDT'], rt)
    n_cold = len(ad.calls)
    assert n_cold == 2
    lab = feed.labels(rt)
    assert set(lab) == {'GOOD/USDT', 'FLAT/USDT'}
    assert lab['GOOD/USDT']['p12_mae'] > 0.07          # 窗内 8% 上偏被 mae 捕获
    assert lab['FLAT/USDT']['p12_cross1'] == 0.0
    # 下一小时增量:since 从缓冲尾回退 ≤2min,不再全量
    rt2 = rt + pd.Timedelta(hours=1)
    feed.update(['GOOD/USDT', 'FLAT/USDT'], rt2)
    sym, start_ms, _ = ad.calls[n_cold]
    gap_min = (rt2 - pd.Timestamp(start_ms, unit='ms')).total_seconds() / 60
    assert gap_min <= 63                                # 增量而非 13h 全量


def test_missing_coin_excluded_and_pool_trim():
    class DeadAdapter(FakeAdapter):
        def fetch_ohlcv(self, sym, tf, s, e):
            if sym == 'DEAD/USDT':
                raise RuntimeError('boom')
            return super().fetch_ohlcv(sym, tf, s, e)
    ad = DeadAdapter()
    feed = LabelFeed(ad, pace_ms=0, cold_pace_ms=0, sleep=lambda s: None)
    rt = pd.Timestamp('2026-07-27 03:00:00')
    feed.update(['DEAD/USDT', 'FLAT/USDT'], rt)
    assert set(feed.labels(rt)) == {'FLAT/USDT'}       # fail-open:缺数据币缺标签不参选
    feed.update(['FLAT/USDT'], rt + pd.Timedelta(hours=1))
    assert set(feed._buf) == {'FLAT/USDT'}             # 掉出票池的缓冲被修剪
