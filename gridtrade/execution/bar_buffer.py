"""per-symbol 已收盘 1m 滚动缓冲：冷载一次全窗，之后只拉增量；取数失败沿用旧缓冲。
只留 candle_begin_time < floor(now,'1min') 的已收盘 bar（丢 forming 半截桶）。"""
import time

import pandas as pd

_MIN_MS = 60_000


class OneMinuteBarBuffer:
    def __init__(self, fetch_fn, window_ms, now_fn=time.time, log=print):
        self.fetch_fn = fetch_fn          # (symbol, since_ms, until_ms) -> DataFrame
        self.window_ms = int(window_ms)
        self._now = now_fn
        self.log = log
        self._buf = {}                    # symbol -> DataFrame(已收盘, 升序)

    @staticmethod
    def _closed(df, cutoff):
        if df is None or len(df) == 0 or 'candle_begin_time' not in df.columns:
            return pd.DataFrame()
        return df[df['candle_begin_time'] < cutoff].copy()

    def get_closed_bars(self, symbol):
        now_ms = int(self._now() * 1000)
        cutoff = pd.Timestamp(now_ms, unit='ms').floor('min')   # 当前 forming 分钟起点
        cutoff_ms = int(cutoff.value // 1_000_000)
        lo_ms = cutoff_ms - self.window_ms                      # 窗口按收盘分钟边界对齐
        buf = self._buf.get(symbol)
        stale = (buf is None or buf.empty
                 or int(buf['candle_begin_time'].iloc[-1].value // 1_000_000) < lo_ms)
        try:
            if stale:
                df = self.fetch_fn(symbol, lo_ms, now_ms)
                buf = self._closed(df, cutoff)
            else:
                last_ms = int(buf['candle_begin_time'].iloc[-1].value // 1_000_000)
                inc = self._closed(self.fetch_fn(symbol, last_ms + _MIN_MS, now_ms), cutoff)
                if not inc.empty:
                    buf = (pd.concat([buf, inc], ignore_index=True)
                           .drop_duplicates('candle_begin_time')
                           .sort_values('candle_begin_time'))
        except Exception as exc:            # 降级：沿用旧缓冲，绝不塌回空
            self.log('[bar_buffer] %s fetch 降级,沿用缓冲: %r' % (symbol, exc))
            if buf is None:
                return pd.DataFrame()
        if buf is None or buf.empty:
            return pd.DataFrame()
        lo = pd.Timestamp(lo_ms, unit='ms')
        buf = buf[(buf['candle_begin_time'] >= lo)
                  & (buf['candle_begin_time'] < cutoff)].reset_index(drop=True)
        self._buf[symbol] = buf
        return buf
