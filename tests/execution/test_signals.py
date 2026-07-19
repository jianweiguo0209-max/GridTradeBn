"""LiveSignalProvider：pv_spike 对齐 calc_pv_spike + funding 取最新 + 节流缓存 + 失败降级。"""
import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import calc_pv_spike
from gridtrade.execution.signals import LiveSignalProvider


def _bars_with_spike(n=120):
    t = pd.date_range('2026-06-01', periods=n, freq='1min')
    qv = np.full(n, 1e5, dtype=float)
    qv[-15:] = 2e6            # 末段 15min 量能尖峰
    return pd.DataFrame({'candle_begin_time': t, 'open': 100.0, 'high': 100.0,
                         'low': 100.0, 'close': 100.0, 'quote_volume': qv})


def _funding(rates):
    ts = [1_700_000_000_000 + i * 3600_000 for i in range(len(rates))]
    return pd.DataFrame({'ts': ts, 'symbol': 'X', 'fundingRate': rates, 'realizedRate': rates})


class FakeAdapter:
    def __init__(self, bars=None, funding=None, raise_ohlcv=False, raise_funding=False):
        self._bars = bars
        self._funding = funding
        self.raise_ohlcv = raise_ohlcv
        self.raise_funding = raise_funding
        self.ohlcv_calls = 0
        self.funding_calls = 0

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        self.ohlcv_calls += 1
        self.last_ohlcv = (symbol, timeframe, int(start_ms), int(end_ms))
        if self.raise_ohlcv:
            raise RuntimeError('boom')
        return self._bars

    def fetch_funding_history(self, symbol, start_ms, end_ms):
        self.funding_calls += 1
        self.last_funding = (int(start_ms), int(end_ms))
        if self.raise_funding:
            raise RuntimeError('boom')
        return self._funding


def test_pv_spike_matches_calc_pv_spike_and_latest_funding():
    bars = _bars_with_spike()
    expect_pv = int(calc_pv_spike(bars, active_period='15min', mult=3, n=233)['pv_spike'].iloc[-1])
    assert expect_pv == 1                       # 构造的尖峰确实触发（否则测试无意义）
    adp = FakeAdapter(bars=bars, funding=_funding([0.0001, 0.0005, 0.0012]))
    prov = LiveSignalProvider(adp, mult=3, period='15min', n=233, now_fn=lambda: 1000.0)
    pv, fr = prov.get('g1', 'X/USDC:USDC', open_ms=0)
    assert pv == expect_pv
    assert abs(fr - 0.0012) < 1e-12             # 取最新一条 fundingRate


def test_throttle_reuses_cache_within_refresh():
    now = {'t': 1000.0}
    adp = FakeAdapter(bars=_bars_with_spike(), funding=_funding([0.001]))
    prov = LiveSignalProvider(adp, refresh_sec=900, now_fn=lambda: now['t'])
    prov.get('g1', 'X', 0)
    assert adp.ohlcv_calls == 1
    now['t'] = 1000.0 + 800                      # < refresh_sec → 命中缓存、不再取数
    prov.get('g1', 'X', 0)
    assert adp.ohlcv_calls == 1 and adp.funding_calls == 1
    now['t'] = 1000.0 + 901                      # 超过 refresh_sec → 重新取数
    prov.get('g1', 'X', 0)
    assert adp.ohlcv_calls == 2


def test_failure_degrades_to_safe_defaults():
    adp = FakeAdapter(raise_ohlcv=True, raise_funding=True)
    prov = LiveSignalProvider(adp, now_fn=lambda: 1.0, log=lambda *a: None)
    pv, fr = prov.get('g1', 'X', 0)
    assert pv == 0 and fr == 0.0                 # 取数异常→安全默认，不抛


def test_empty_data_returns_zero():
    adp = FakeAdapter(bars=pd.DataFrame(), funding=pd.DataFrame())
    prov = LiveSignalProvider(adp, now_fn=lambda: 1.0)
    assert prov.get('g1', 'X', 0) == (0, 0.0)


def test_evict_removes_cache_entry():
    adp = FakeAdapter(bars=_bars_with_spike(), funding=_funding([0.001]))
    prov = LiveSignalProvider(adp, now_fn=lambda: 1.0)
    prov.get('g1', 'X', 0)
    assert 'g1' in prov._cache
    prov.evict('g1')
    assert 'g1' not in prov._cache
    prov.evict('missing')            # 缺失也安全、不抛


def _bars_1m(n=1620, base_qv=1e5, spike_qv=None, spike_len=15):
    """n 根 1m。spike_qv 非空时把**最后 spike_len 根**换成尖峰量(=滚动窗刚好装满)。"""
    t = pd.date_range('2026-06-01', periods=n, freq='1min')
    qv = np.full(n, base_qv, dtype=float)
    if spike_qv is not None:
        qv[-spike_len:] = spike_qv
    return pd.DataFrame({'candle_begin_time': t, 'open': 100.0, 'high': 100.0,
                         'low': 100.0, 'close': 100.0, 'quote_volume': qv})


def test_fetch_is_1m_lookback_decoupled_from_open_ms():
    """方案C(2026-07-18):取数粒度=**原生 1m**,窗口=now−(n+8)×period,与 open_ms 解耦。

    calc_pv_spike 现在算「截至 now 的滚动 period 窗」,需要 period 内的细粒度成交额。
    此前取原生 15m → 该函数的 resample 退化成空操作 → iloc[-1] 拿到的是**进行中的半截桶**,
    而回测那侧是**整桶(含未来)**广播 —— 两侧从不同源(回测 67.2% 的格窗见尖峰 vs 实盘 20.6%)。
    """
    adp = FakeAdapter(bars=_bars_1m(), funding=_funding([0.001]))
    prov = LiveSignalProvider(adp, mult=3, period='15min', n=100, now_fn=lambda: 1_000_000.0)
    now_ms = 1_000_000_000
    prov.get('g1', 'X', open_ms=now_ms - 60_000)      # 开格才 1 分钟
    sym, tf, start, end = adp.last_ohlcv
    assert tf == '1m', '滚动窗需 1m 粒度;取 15m 会退化回半截桶语义'
    assert end == now_ms
    assert start == now_ms - 108 * 900_000            # (n+8)×period,与 open_ms 无关

    prov2 = LiveSignalProvider(adp, mult=3, period='15min', n=100, now_fn=lambda: 1_000_000.0)
    prov2.get('g2', 'X', open_ms=0)                   # 开格很久
    assert adp.last_ohlcv[2] == now_ms - 108 * 900_000  # 窗口不随 open_ms 变


def test_full_window_baseline_detects_spike_vs_long_history():
    """满窗行为差分:1605 根低量历史 + 末 15 根(=一个滚动窗)5×爆量 → 满窗基线判出尖峰。
    (最初实现只取「开网→现在」,开格 1 分钟时基线=自身 expanding,永远判不出。)"""
    bars = _bars_1m(n=1620, base_qv=1e5, spike_qv=5e5)   # 5×基线 > mult=3
    adp = FakeAdapter(bars=bars, funding=_funding([0.001]))
    prov = LiveSignalProvider(adp, mult=3, period='15min', n=100, now_fn=lambda: 1_000_000.0)
    pv, _ = prov.get('g1', 'X', open_ms=999_940_000)    # 开格才 1 分钟
    assert pv == 1


def test_funding_rate_lookback_matches_settlement_interval_plus_1h():
    """回看窗=结算周期+1h(终审 Important 4)：FakeAdapter 无 FUNDING_INTERVAL_HOURS→
    getattr 默认 8h→9h 窗，而非旧固定 3h。7h 前的最新费率行落在 9h 窗内被取到；
    旧 3h 窗（[now-3h, now]）早于该行时间戳，会漏判、悄悄退回默认值 0.0。"""
    now_ms = 1_700_000_000_000 + 7 * 3600_000       # "现在" = 该费率行之后 7h
    row_ts = 1_700_000_000_000
    funding = pd.DataFrame({'ts': [row_ts], 'symbol': 'X',
                           'fundingRate': [0.00042], 'realizedRate': [0.00042]})
    adp = FakeAdapter(bars=_bars_with_spike(), funding=funding)
    prov = LiveSignalProvider(adp, now_fn=lambda: now_ms / 1000.0)
    _, fr = prov.get('g1', 'X', open_ms=0)
    start_ms, end_ms = adp.last_funding
    assert end_ms - start_ms == 9 * 3600_000         # 8h 结算 + 1h,非旧固定 3h
    assert start_ms <= row_ts <= end_ms               # 7h 前的行落在 9h 窗内
    assert row_ts < now_ms - 3 * 3600_000             # 旧固定 3h 窗会早于该行,漏判
    assert abs(fr - 0.00042) < 1e-12                  # 确实取到该行,而非默认 0.0
