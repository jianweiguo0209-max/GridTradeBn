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


class FlakyFetch(RecordingFetch):
    def __init__(self, full):
        super().__init__(full)
        self.fail_after = None

    def __call__(self, symbol, since_ms, until_ms):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            self.calls.append((symbol, int(since_ms), int(until_ms)))
            raise RuntimeError('boom')
        return super().__call__(symbol, since_ms, until_ms)


def test_incremental_failure_keeps_existing_buffer():
    full = _series(400)
    fetch = FlakyFetch(full)
    t1 = _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t1), log=lambda *a: None)
    first = buf.get_closed_bars('X')
    assert len(first) == 100
    fetch.fail_after = 1                                  # 之后所有拉都抛
    t2 = _START + pd.Timedelta(minutes=201) + pd.Timedelta(seconds=5)
    buf._now = _now_fn_at(t2)
    bars = buf.get_closed_bars('X')                       # 增量拉失败
    assert not bars.empty                                 # 沿用缓冲,不塌回 0
    assert bars['candle_begin_time'].max() == _START + pd.Timedelta(minutes=199)


def test_cold_load_failure_with_empty_buffer_returns_empty():
    fetch = FlakyFetch(_series(200))
    fetch.fail_after = 0                                  # 第一次冷载就抛
    t = _START + pd.Timedelta(minutes=150) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t), log=lambda *a: None)
    assert buf.get_closed_bars('X').empty                 # 无缓冲 + 拉失败 → 空,不抛


def test_long_downtime_triggers_cold_reload():
    full = _series(1000)
    fetch = RecordingFetch(full)
    t1 = _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t1), log=lambda *a: None)
    buf.get_closed_bars('X')
    t2 = _START + pd.Timedelta(minutes=600) + pd.Timedelta(seconds=5)   # 跳 400 分钟 > 窗宽100
    buf._now = _now_fn_at(t2)
    buf.get_closed_bars('X')
    # 缓冲最后 ts 已早于 now-window → 走冷载全窗(since=cutoff对齐-window),而非增量
    lo_ms = int((t2.floor('min') - pd.Timedelta(minutes=100)).value // 1_000_000)
    assert fetch.calls[-1][1] == lo_ms


def test_fresh_buffer_short_circuits_fetch():
    """同一分钟内重复调用（同币双格场景）：缓冲已含最新收盘桶 → 零新增请求、结果逐字节一致。"""
    full = _series(400)
    fetch = RecordingFetch(full)
    t1 = _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000, now_fn=_now_fn_at(t1))
    first = buf.get_closed_bars('X')
    n_calls = len(fetch.calls)                       # 冷载 1 次
    second = buf.get_closed_bars('X')                # 同分钟再调
    assert len(fetch.calls) == n_calls               # 短路：零新增 fetch
    pd.testing.assert_frame_equal(first, second)     # 数据完全同源


def test_fresh_short_circuit_unsticks_after_minute_rollover():
    """分钟翻转后必须恢复增量取数（防过度缓存吃掉新收盘桶）。"""
    full = _series(400)
    fetch = RecordingFetch(full)
    now = {'ts': _START + pd.Timedelta(minutes=200) + pd.Timedelta(seconds=5)}
    buf = OneMinuteBarBuffer(fetch, window_ms=100 * 60_000,
                             now_fn=lambda: now['ts'].value / 1e9)
    buf.get_closed_bars('X')
    n_calls = len(fetch.calls)
    now['ts'] += pd.Timedelta(minutes=1)             # 进入下一分钟
    bars = buf.get_closed_bars('X')
    assert len(fetch.calls) == n_calls + 1           # 恢复增量 fetch
    assert bars['candle_begin_time'].max() == _START + pd.Timedelta(minutes=200)
