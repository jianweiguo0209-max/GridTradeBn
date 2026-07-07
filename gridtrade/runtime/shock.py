"""MarketShockBrake 信号(spec 2026-07-08-market-shock-brake):
票池横截面中位数 k 小时收益。回测验证 GO(memory shock-brake-verdict:
k=4h/thr=4%/X=2h,事件捕获 37/37、四窗Δ收益合计 0、被拦格基线净亏)。
纯函数、fail-open:数据不足返回 None(调用方不刹车,不阻塞主流程)。"""
import pandas as pd

MIN_BASKET = 5    # 有效币少于此 → None(fail-open;冷启动/坏数据日不误刹)


def cross_median_k(candles, run_time, k_hours, *, min_basket=MIN_BASKET):
    """candles: {symbol: 1h df(candle_begin_time, close)}(scheduler 本轮已拉的票池 K 线)。
    返回 中位数(close[-1]/close[-1-k] − 1),PIT:只用 candle_begin_time < run_time 的 bar;
    有效币 < min_basket 或数据异常 → None。"""
    cutoff = pd.Timestamp(run_time)
    rets = []
    for df in (candles or {}).values():
        try:
            if df is None or df.empty or 'close' not in df.columns:
                continue
            sub = df[df['candle_begin_time'] < cutoff]
            if len(sub) < k_hours + 1:
                continue
            closes = sub['close'].astype(float)
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-1 - k_hours])
            if prev > 0:
                rets.append(last / prev - 1.0)
        except Exception:
            continue                     # 单币坏数据不阻塞篮子
    if len(rets) < min_basket:
        return None
    return float(pd.Series(rets).median())
