"""回测侧 MarketShockBrake 信号(spec 2026-07-08-market-shock-brake 的回测同步)。

与实盘 `gridtrade.runtime.shock.cross_median_k` **同一数学、两种算形**:实盘逐 rt 从当轮
candles 算一个标量;回测把整窗向量化成序列(同源守卫测试钉逐位一致,见
tests/backtest/test_shock_replay.py)。语义(与实盘/研究 harness 一致):
  med_k(rt) = 票池横截面中位数 k 小时收益(PIT:只用 candle_begin_time < rt 的 bar;
              篮子 = trailing 24 根 1h quote_volume 和 ≥ floor);
  fired(rt) = |med_k| ≥ thr;blocked(rt) = [rt−X+1h, rt] 内任一评估时点 fired(=封锁 [t, t+X))。
回测验证 GO 配置 k=4h/thr=4%/X=2h(memory shock-brake-verdict)。"""
import pandas as pd


def median_signal_series(series_1h, k_hours, *, min_quote_volume=0.0):
    """series_1h: {symbol: 1h df(candle_begin_time/close[/quote_volume])}。
    返回 med_k 序列,index=评估时点(bar 收盘后 = bar_begin + 1h)。
    min_quote_volume>0 时按 trailing 24 根 qv 和过滤篮子(缺 qv 列的币按不过地板处理)。"""
    closes, qvs = {}, {}
    for s, df in (series_1h or {}).items():
        if df is None or df.empty or 'close' not in df.columns:
            continue
        d = df.drop_duplicates('candle_begin_time').set_index('candle_begin_time').sort_index()
        closes[s] = d['close'].astype(float)
        if 'quote_volume' in d.columns:
            qvs[s] = d['quote_volume'].astype(float)
    if not closes:
        return pd.Series(dtype=float)
    C = pd.DataFrame(closes).sort_index()
    ret = C / C.shift(k_hours) - 1.0
    if min_quote_volume and min_quote_volume > 0:
        Q = pd.DataFrame(qvs).reindex(columns=C.columns, index=C.index)
        basket = Q.rolling(24, min_periods=24).sum() >= float(min_quote_volume)
        ret = ret.where(basket)
    med = ret.median(axis=1, skipna=True)
    med.index = med.index + pd.Timedelta(hours=1)     # bar 收盘后才可见 → 评估时点后移 1h
    return med


def blocked_index(med, thr, x_hours, rts):
    """blocked(rt) = [rt−X+1h, rt] 内任一评估时点 |med|≥thr。返回 rts 上的 bool Series。
    fired 先扩展到 med.index ∪ rts 再滚动——否则封锁窗越过信号序列尾部的拖尾会被丢
    (fired 在序列末小时时,其后 X−1 小时的 rts 也须封锁)。"""
    rts = pd.DatetimeIndex(rts)
    fired = (med.abs() >= float(thr)).astype(int)
    idx = fired.index.union(rts)
    fired = fired.reindex(idx).fillna(0)
    blk = fired.rolling(int(x_hours), min_periods=1).max()
    return blk.reindex(rts).fillna(0).astype(bool)


def blocked_rts(cache, universe, window_start, window_end, timeframe, k_hours, thr,
                pause_hours, *, min_quote_volume=0.0):
    """便捷封装:从缓存载 universe 1h → 整窗 blocked rt 集合(供 run_backtest 接线)。"""
    from gridtrade.backtest import selection_replay as SR
    series = SR.load_full_series(cache, universe, timeframe)
    med = median_signal_series(series, k_hours, min_quote_volume=min_quote_volume)
    rts = pd.date_range(window_start, window_end, freq='1H')
    blk = blocked_index(med, thr, pause_hours, rts)
    return set(rts[blk.values])
