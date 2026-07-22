import pandas as pd
from gridtrade.execution.bar_buffer import OneMinuteBarBuffer

_START = pd.Timestamp('2026-06-01 00:00')


def _series(n_min, base=1e5):
    t = pd.date_range(_START, periods=n_min, freq='1min')
    return pd.DataFrame({'candle_begin_time': t, 'open': 100.0, 'high': 100.0,
                         'low': 100.0, 'close': 100.0, 'quote_volume': float(base)})


class RecordingFetch:
    """按 [since_ms, until_ms] 切片返回（含末尾 forming 桶，镜像币安）。"""
    def __init__(self, full):
        self.full = full
        self.calls = []

    def __call__(self, symbol, since_ms, until_ms):
        self.calls.append((symbol, int(since_ms), int(until_ms)))
        s = pd.Timestamp(int(since_ms), unit='ms')
        u = pd.Timestamp(int(until_ms), unit='ms')
        m = (self.full['candle_begin_time'] >= s) & (self.full['candle_begin_time'] <= u)
        return self.full[m].copy()


def _now_fn_at(ts):
    return lambda: ts.value / 1e9      # pandas ns → 秒


def test_cold_load_fetches_full_window_and_drops_forming_bar():
    full = _series(200)                                  # 00:00..03:19
    fetch = RecordingFetch(full)
    now = _START + pd.Timedelta(minutes=150) + pd.Timedelta(seconds=20)   # 02:30:20（02:30 桶未收盘）
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(now))
    bars = buf.get_closed_bars('X')
    assert len(fetch.calls) == 1                         # 冷载一次
    assert bars['candle_begin_time'].max() == _START + pd.Timedelta(minutes=149)  # 02:29=最后已收盘
    assert (_START + pd.Timedelta(minutes=150)) not in set(bars['candle_begin_time'])  # forming 桶被丢
    assert len(bars) == 100                              # 窗宽=100 根


def test_incremental_only_fetches_new_bars_and_equals_full_reload():
    full = _series(400)
    fetch = RecordingFetch(full)
    t1 = _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t1))
    buf.get_closed_bars('X')
    last_ts_ms = int((_START + pd.Timedelta(minutes=199)).value // 1_000_000)
    t2 = _START + pd.Timedelta(minutes=205) + pd.Timedelta(seconds=5)     # 前进 5 分钟
    buf._now = _now_fn_at(t2)
    bars = buf.get_closed_bars('X')
    assert fetch.calls[1][1] == last_ts_ms + 60_000      # 增量 since = 上次最后收盘 + 1min
    # 与在 t2 全新冷载等价
    fresh = OneMinuteBarBuffer(RecordingFetch(full), window_ms=100 * 60_000, now_fn=_now_fn_at(t2))
    exp = fresh.get_closed_bars('X')
    assert bars['candle_begin_time'].tolist() == exp['candle_begin_time'].tolist()
