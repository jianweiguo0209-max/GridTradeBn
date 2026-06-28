"""DataSource：基于 ExchangeAdapter + ParquetCache 的回测取数层。
区间按 UTC 天缓存；全部天命中即离线（不触 adapter），缺失天才拉取。
只经 adapter 访问交易所，不直接调 ccxt。"""
import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS, FUNDING_COLS


def _days(start_ms, end_ms):
    s = pd.to_datetime(start_ms, unit='ms').normalize()
    e = pd.to_datetime(end_ms, unit='ms').normalize()
    return [d.strftime('%Y-%m-%d') for d in pd.date_range(s, e, freq='D')]


def _day_bounds_ms(day):
    d0 = pd.Timestamp(day)
    return int(d0.value // 1_000_000), int((d0 + pd.Timedelta(days=1)).value // 1_000_000) - 1


class DataSource:
    def __init__(self, adapter, cache, *, timeframe='1h'):
        self.adapter = adapter
        self.cache = cache
        self.timeframe = timeframe

    def list_instruments(self):
        return self.adapter.list_instruments()

    def _warm(self, namespace, symbol, start_ms, end_ms, fetch_fn, cols, time_col):
        days = _days(start_ms, end_ms)
        missing = [d for d in days if not self.cache.exists(namespace, symbol, d)]
        if missing:
            lo, _ = _day_bounds_ms(missing[0])
            _, hi = _day_bounds_ms(missing[-1])
            fetched = fetch_fn(symbol, lo, hi)
            for d in missing:
                d_lo, d_hi = _day_bounds_ms(d)
                if fetched.empty:
                    self.cache.write_empty(namespace, symbol, d, cols)
                    continue
                ms = (fetched[time_col].astype('int64') if time_col == 'ts'
                      else fetched[time_col].view('int64') // 1_000_000)
                day_df = fetched[(ms >= d_lo) & (ms <= d_hi)]
                if day_df.empty:
                    self.cache.write_empty(namespace, symbol, d, cols)
                else:
                    self.cache.write(namespace, symbol, d, day_df.reset_index(drop=True))
        frames = [self.cache.read(namespace, symbol, d) for d in days]
        frames = [f for f in frames if f is not None and not f.empty]
        if not frames:
            return pd.DataFrame(columns=cols)
        return pd.concat(frames, ignore_index=True)

    def fetch_ohlcv_range(self, symbol, start_ms, end_ms):
        df = self._warm(self.timeframe, symbol, start_ms, end_ms,
                        lambda s, lo, hi: self.adapter.fetch_ohlcv(s, self.timeframe, lo, hi),
                        CANDLE_COLS, 'candle_begin_time')
        if df.empty:
            return df
        ms = df['candle_begin_time'].view('int64') // 1_000_000
        df = df[(ms >= start_ms) & (ms <= end_ms)]
        return df.sort_values('candle_begin_time').drop_duplicates(
            subset=['candle_begin_time']).reset_index(drop=True)

    def fetch_funding_range(self, symbol, start_ms, end_ms):
        df = self._warm('funding', symbol, start_ms, end_ms,
                        lambda s, lo, hi: self.adapter.fetch_funding_history(s, lo, hi),
                        FUNDING_COLS, 'ts')
        if df.empty:
            return df
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        return df.sort_values('ts').drop_duplicates(subset=['ts']).reset_index(drop=True)
