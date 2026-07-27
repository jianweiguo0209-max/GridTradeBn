import pandas as pd

from gridtrade.runtime.label_feed import LabelFeed


class FakeAdapter:
    """合成 1m 行情:恒价 100,仅 GOOD/USDT 在窗内某分钟拉高 8%。记录取数区间和权重调用。"""
    def __init__(self):
        self.calls = []
        self.weight_calls = 0

    def report_weight(self):
        """权重遥测钩子,每次取数前调用。"""
        self.weight_calls += 1

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


def test_report_weight_called_for_every_symbol_including_failing():
    """回归测试:report_weight()在每个符号前调用,包括会raise的那个。"""
    class DeadAdapter(FakeAdapter):
        def fetch_ohlcv(self, sym, tf, s, e):
            if sym == 'DEAD/USDT':
                raise RuntimeError('boom')
            return super().fetch_ohlcv(sym, tf, s, e)

    ad = DeadAdapter()
    feed = LabelFeed(ad, pace_ms=0, cold_pace_ms=0, sleep=lambda s: None)
    rt = pd.Timestamp('2026-07-27 03:00:00')
    feed.update(['GOOD/USDT', 'DEAD/USDT', 'FLAT/USDT'], rt)
    # report_weight()必须被调用3次(每个符号一次),即使 DEAD/USDT raise
    assert ad.weight_calls == 3


def test_pace_ms_and_cold_pace_ms_respected():
    """回归测试:i==0 不sleep,冷启动用 cold_pace_ms,增量用 pace_ms。"""
    sleep_calls = []

    ad = FakeAdapter()
    # 第一轮冷启动: 符号0不sleep, 符号1冷启(cold_pace_ms=800ms), 符号2冷启
    feed = LabelFeed(ad, pace_ms=300.0, cold_pace_ms=800.0,
                     sleep=lambda s: sleep_calls.append(s))
    rt = pd.Timestamp('2026-07-27 03:00:00')
    feed.update(['SYM1/USDT', 'SYM2/USDT', 'SYM3/USDT'], rt)

    # 第一轮: 符号0 no sleep, 符号1 cold(0.8), 符号2 cold(0.8)
    assert sleep_calls == [0.8, 0.8], f"cold start sleep calls: {sleep_calls}"

    sleep_calls.clear()
    # 第二轮增量: 符号0不sleep(i==0), 符号1增量(pace=0.3), 符号2增量(pace=0.3)
    rt2 = rt + pd.Timedelta(hours=1)
    feed.update(['SYM1/USDT', 'SYM2/USDT', 'SYM3/USDT'], rt2)

    # 第二轮: 符号0 no sleep, 符号1 warm(0.3), 符号2 warm(0.3)
    assert sleep_calls == [0.3, 0.3], f"incremental sleep calls: {sleep_calls}"


def test_pool_trim_evicts_populated_entries():
    """回归测试:掉出票池的符号,其缓冲即使populated也被删除。"""
    ad = FakeAdapter()
    feed = LabelFeed(ad, pace_ms=0, cold_pace_ms=0, sleep=lambda s: None)
    rt = pd.Timestamp('2026-07-27 03:00:00')

    # 第一轮: A+B 都loaded into buffer
    feed.update(['COIN_A/USDT', 'COIN_B/USDT'], rt)
    assert set(feed._buf) == {'COIN_A/USDT', 'COIN_B/USDT'}
    assert len(feed._buf['COIN_A/USDT']) > 0  # populated
    assert len(feed._buf['COIN_B/USDT']) > 0  # populated

    # 第二轮: 只更新 A, B 应该被修剪掉
    rt2 = rt + pd.Timedelta(hours=1)
    feed.update(['COIN_A/USDT'], rt2)
    assert set(feed._buf) == {'COIN_A/USDT'}  # B trimmed even though it was populated
