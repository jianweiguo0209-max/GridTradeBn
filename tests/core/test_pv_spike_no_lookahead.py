"""pv_spike 口径:截至 t 的滚动窗,无前视(方案C,2026-07-18)。

旧口径的 bug:`resample(active_period)` 用**桶起点**贴标签,`merge_asof(direction='backward')`
把**整桶**(含未来)算出的信号广播回桶内每一根 bar → **前视最多一个 active_period**。
实证:尖峰真实发生在 10:10,而 10:00 那根 bar 的 pv_spike 已经是 1。
pv 主动止损是回测 53.9% 的退出路径(第一大),故这条前视污染的是全系统最大单项。

而实盘 `signals.py` 取的是**进行中的半截桶**(`iloc[-1]`,成交额只累到当下)——两侧从不同源:
回测 67.2% 的格窗见到尖峰,实盘只有 20.6%(丢 69%)。且实盘按 refresh_sec 节流、起点是开格
时刻而非桶边界,scheduler 整点唤醒 → 整个 12h 都卡在桶内第 1-7 分钟采样,命中率 0.16%。

方案C = 两侧统一为「截至 t 的滚动窗」:
  cur(t)  = (t-active_period, t] 的成交额     —— 无前视
  base(t) = 过去 n 个同口径窗的均量           —— 无前视
副产品:窗宽 = active_period → 信号在尖峰后**粘住一个 period**,实盘按 refresh_sec(=period)
采样必能命中 → **相位锁问题一并消失**。
"""
import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import calc_pv_spike

_SPIKE_AT = pd.Timestamp('2026-01-01 10:10:00')


def _bars(quiet=100.0, spike=100000.0):
    """90 根 1m,平静量能 100,只有 10:10 那根是 100000 的尖峰。"""
    t = pd.date_range('2026-01-01 09:00:00', periods=90, freq='1min')
    qv = np.full(90, quiet)
    qv[list(t).index(_SPIKE_AT)] = spike
    return pd.DataFrame({'candle_begin_time': t, 'quote_volume': qv})


def _sig(**kw):
    out = calc_pv_spike(_bars(), active_period='15min', mult=3, n=10, **kw)
    return out.set_index('candle_begin_time')['pv_spike']


def test_no_lookahead_before_spike():
    """核心:尖峰在 10:10,则 10:10 **之前**任何一根都不得报警。旧口径下 10:00~10:09 全是 1。"""
    m = _sig()
    before = m[m.index < _SPIKE_AT]
    fired = before[before == 1]
    assert len(fired) == 0, '前视!尖峰前就报警的时点: %s' % list(fired.index.astype(str))


def test_spike_detected_when_it_lands():
    """尖峰落地那一刻必须报警(否则是把信号改废了,不是修好了)。"""
    assert int(_sig().loc[_SPIKE_AT]) == 1


def test_signal_sticks_for_one_period_after_spike():
    """滚动窗宽 = active_period → 尖峰后信号粘住整整一个 period。
    这正是干掉实盘相位锁的性质:实盘每 refresh_sec(=period) 采样一次,必有一次落在粘滞区内。"""
    m = _sig()
    window = m[(m.index >= _SPIKE_AT) & (m.index < _SPIKE_AT + pd.Timedelta('15min'))]
    assert len(window) == 15
    assert (window == 1).all(), '粘滞不足,实盘按 period 采样会漏掉尖峰'


def test_signal_clears_after_window_slides_past():
    """尖峰滑出滚动窗后必须归零(否则会无限误触发)。"""
    m = _sig()
    after = m[m.index >= _SPIKE_AT + pd.Timedelta('15min')]
    assert len(after) > 0 and (after == 0).all()


def test_quiet_market_never_fires():
    """无尖峰则全程 0(基线自洽性:平稳量能不该超过自身均值的 mult 倍)。"""
    t = pd.date_range('2026-01-01 09:00:00', periods=90, freq='1min')
    flat = pd.DataFrame({'candle_begin_time': t, 'quote_volume': np.full(90, 100.0)})
    out = calc_pv_spike(flat, active_period='15min', mult=3, n=10)
    assert (out['pv_spike'] == 0).all()


def test_missing_quote_volume_returns_none():
    """契约不变:缺 quote_volume 返回 None(调用方据此降级)。"""
    t = pd.date_range('2026-01-01 09:00:00', periods=5, freq='1min')
    assert calc_pv_spike(pd.DataFrame({'candle_begin_time': t}), active_period='15min') is None
