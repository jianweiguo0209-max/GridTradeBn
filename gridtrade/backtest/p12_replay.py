"""回测侧 p12 标签供给 —— 实盘 LabelFeed 的对应物,共用 `core.p12_labels` 的同一数学。

**同源链**:实盘 `runtime/label_feed.py` 与本模块都只调 `core.p12_labels`;逐窗版
`window_label` 与批量版 `window_labels_batch` 由 parity 测试逐位钉死 ⇒ 三处一套数学。

**为什么不复用 LabelFeed**:它按"每轮增量拉交易所"组织(在线),回测是"一次读整段历史、
按轮切窗"(离线)。形状不同、数学相同——这正是 stage_L(holdout_gate)当年的形状。

窗口口径与实盘逐字一致:标签窗 = **[rt−12h, rt)**;窗内 1m bar < 600 ⇒ 该 (rt,币) 无标签
⇒ 不参选(实盘是 inner merge 剔除,回测同)。读 1m 时向前多取 PREHEAT_HOURS,保证窗首根的
positional 前驱在场(实盘靠 13h 缓冲留的那 1h 余量,这里同理)。
"""
import pandas as pd

from gridtrade.core.p12_labels import LABEL_HOURS, p12_eff, window_labels_batch

PREHEAT_HOURS = 1        # 窗前余量:保证 dstep 的窗首前驱在场(同实盘缓冲的那 1h 富余)
# ⚠ LABEL_HOURS 从 core.p12_labels 引入,**不在这里重定义**——它一度在实盘/回测各写一遍。


def build_p12_labels(cache, symbols, run_times, *, log=print):
    """返回 {(run_time, symbol): p12_eff}。缺 1m / 窗内不足 600 根的 (轮,币) 不入表。

    每币只读一次 1m(按窗切天),再对全部 run_time 批量切窗——O(币) 次磁盘读,不是 O(币×轮)。
    """
    rts = [pd.Timestamp(t) for t in run_times]
    if not rts:
        return {}
    lo = (min(rts) - pd.Timedelta(hours=LABEL_HOURS + PREHEAT_HOURS)).date()
    hi = max(rts).date()
    starts = [rt - pd.Timedelta(hours=LABEL_HOURS) for rt in rts]
    out, n_sym, n_miss = {}, 0, 0
    for sym in symbols:
        df = cache.read_days_range('1m', sym, str(lo), str(hi))
        if df is None or df.empty:
            n_miss += 1
            continue
        df = (df[['candle_begin_time', 'close', 'high', 'low']]
              .sort_values('candle_begin_time')
              .drop_duplicates(subset=['candle_begin_time'], keep='last')
              .reset_index(drop=True))
        n_sym += 1
        for rt, r in zip(rts, window_labels_batch(df, starts, hours=LABEL_HOURS)):
            if r is not None:
                out[(rt, sym)] = p12_eff(r[0], r[1])
    log('[p12] 标签 %d 条 | 币 %d 有 1m / %d 缺 | 轮 %d' % (len(out), n_sym, n_miss, len(rts)))
    return out
