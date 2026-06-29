import pandas as pd

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument, CANDLE_COLS

SYM = 'BTC/USDT:USDT'
DAY = 86_400_000


def _bars(start_ms, n_hours):
    ts = [start_ms + i * 3600_000 for i in range(n_hours)]
    return pd.DataFrame({
        'symbol': SYM,
        'candle_begin_time': pd.to_datetime(ts, unit='ms'),
        'open': [1.0] * n_hours, 'high': [2.0] * n_hours, 'low': [0.5] * n_hours,
        'close': [1.5] * n_hours, 'vol': [10.0] * n_hours,
        'volCcy': [10.0] * n_hours, 'quote_volume': [15.0] * n_hours,
    })


def _ds(tmp_path, ex):
    from gridtrade.backtest.cache import ParquetCache
    from gridtrade.backtest.datasource import DataSource
    return DataSource(ex, ParquetCache(str(tmp_path)), timeframe='1h')


def test_fetch_range_warms_cache_then_serves_offline(tmp_path):
    start = 1_704_067_200_000  # 2024-01-01 00:00 UTC
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_ohlcv(SYM, _bars(start, 48))   # 2 天 1h bars
    ds = _ds(tmp_path, ex)
    end = start + 47 * 3600_000
    df1 = ds.fetch_ohlcv_range(SYM, start, end)
    assert list(df1.columns) == CANDLE_COLS and len(df1) == 48

    # 预热后离线：换一个会在 fetch 时报错的交易所，仅靠缓存仍能取到
    class Offline(FakeExchange):
        def fetch_ohlcv(self, *a, **k):
            raise AssertionError('should not hit network after warm')
    off = Offline(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds2 = _ds(tmp_path, off)
    df2 = ds2.fetch_ohlcv_range(SYM, start, end)
    assert len(df2) == 48 and list(df2['close']) == list(df1['close'])


def test_fetch_range_subset_from_cache(tmp_path):
    start = 1_704_067_200_000
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_ohlcv(SYM, _bars(start, 48))
    ds = _ds(tmp_path, ex)
    ds.fetch_ohlcv_range(SYM, start, start + 47 * 3600_000)   # warm 2 days
    sub = ds.fetch_ohlcv_range(SYM, start + 5 * 3600_000, start + 10 * 3600_000)
    assert len(sub) == 6   # inclusive [5h,10h]


def test_list_instruments_passthrough(tmp_path):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds = _ds(tmp_path, ex)
    insts = ds.list_instruments()
    assert insts[0].symbol == SYM


def _funding(start_ms, n, step_ms=8 * 3600_000):
    ts = [start_ms + i * step_ms for i in range(n)]
    return pd.DataFrame({
        'ts': ts, 'symbol': SYM,
        'fundingRate': [0.0001 * (i + 1) for i in range(n)],
        'realizedRate': [0.0001 * (i + 1) for i in range(n)],
    })


class _OfflineFunding(FakeExchange):
    def fetch_funding_history(self, *a, **k):
        raise AssertionError('should not hit network after warm')


def test_funding_range_warms_then_serves_offline(tmp_path):
    from gridtrade.exchanges.base import FUNDING_COLS
    start = 1_704_067_200_000                      # 2024-01-01 00:00 UTC
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_funding(SYM, _funding(start, 6))       # 2 天、8h 间隔 -> 6 行
    ds = _ds(tmp_path, ex)
    end = start + 2 * DAY - 1
    df1 = ds.fetch_funding_range(SYM, start, end)
    assert list(df1.columns) == FUNDING_COLS and len(df1) == 6

    off = _OfflineFunding(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds2 = _ds(tmp_path, off)                        # 同一 cache、触网即报错
    df2 = ds2.fetch_funding_range(SYM, start, end)
    assert len(df2) == 6 and list(df2['fundingRate']) == list(df1['fundingRate'])


def test_funding_range_any_covered_window_fully_offline(tmp_path):
    # 预热后，区间内任意子窗口都应完全由缓存服务、不触网
    start = 1_704_067_200_000
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_funding(SYM, _funding(start, 6))
    ds = _ds(tmp_path, ex)
    ds.fetch_funding_range(SYM, start, start + 2 * DAY - 1)   # warm 2 days
    off = _OfflineFunding(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds2 = _ds(tmp_path, off)
    sub = ds2.fetch_funding_range(SYM, start, start + DAY - 1)  # 第 1 天子窗口
    assert len(sub) == 3 and all(sub['ts'] < start + DAY)       # 仅第 1 天 3 行，纯离线


def test_funding_range_empty_day_sentinel_offline(tmp_path):
    # 某天无资金费 -> 落空哨兵 -> 离线仍正常（不报错、不并入空数据）
    start = 1_704_067_200_000
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_funding(SYM, _funding(start, 3))        # 仅第 1 天有 3 行；第 2 天空
    ds = _ds(tmp_path, ex)
    ds.fetch_funding_range(SYM, start, start + 2 * DAY - 1)   # warm 2 days（第 2 天空哨兵）
    off = _OfflineFunding(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds2 = _ds(tmp_path, off)
    df = ds2.fetch_funding_range(SYM, start, start + 2 * DAY - 1)
    assert len(df) == 3                              # 第 2 天空哨兵不并入垃圾行
