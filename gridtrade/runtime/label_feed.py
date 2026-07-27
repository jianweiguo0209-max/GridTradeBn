"""每币滚动 13h 1m 缓冲 → p12 标签(eff1 选币数据源)。

权重账(实测 2026-07-27,上限 2400/min,基线最差分钟 1436):
  稳态:每轮每币增量 limit≈65 ⇒ weight 1;282 币 × pace 300ms ≈ +200/min。
  冷启动(进程首轮):limit≈780 ⇒ weight 5/币;pace 800ms 摊 ~226s ≈ +375/min。
每次取数前调 adapter.report_weight()(遥测归因,同 scheduler._fetch_pass)。
缓冲 13h、标签窗 12h:1h 余量保证窗首根的 positional 前驱在场(p12_labels docstring)。
fail-open:单币取数失败只跳过 ⇒ 该币本轮缺标签不参选,绝不阻塞选币轮。
"""
import time

import pandas as pd

from gridtrade.core.p12_labels import p12_eff, window_label

BUFFER_HOURS = 13
REFETCH_TAIL_MS = 120_000        # 尾部回拉 2min:治「上轮末根未定型」的陈旧半根


class LabelFeed:
    def __init__(self, adapter, *, pace_ms=300.0, cold_pace_ms=800.0,
                 log=print, sleep=time.sleep):
        self.adapter = adapter
        self.pace_ms = float(pace_ms)
        self.cold_pace_ms = float(cold_pace_ms)
        self.log = log
        self.sleep = sleep
        self._buf = {}

    def update(self, symbols, run_time):
        rt = pd.Timestamp(run_time)
        end_ms = int(rt.value // 1_000_000) - 1          # 排除 begin==rt 的成型中 bar
        lo_ms = int((rt - pd.Timedelta(hours=BUFFER_HOURS)).value // 1_000_000)
        rw = getattr(self.adapter, 'report_weight', None)
        t0, n_cold, n_fail = time.time(), 0, 0
        for i, sym in enumerate(symbols):
            prev = self._buf.get(sym)
            cold = prev is None or prev.empty
            n_cold += int(cold)
            since = lo_ms if cold else max(
                lo_ms, int(prev['ts'].iloc[-1]) - REFETCH_TAIL_MS)
            if i:
                self.sleep((self.cold_pace_ms if cold else self.pace_ms) / 1000.0)
            if rw is not None:
                rw()
            try:
                df = self.adapter.fetch_ohlcv(sym, '1m', since, end_ms)
            except Exception:
                n_fail += 1
                continue                                  # fail-open:缺标签不参选
            if df is None or df.empty:
                continue
            cur = df if cold else pd.concat([prev, df], ignore_index=True)
            cur = (cur.drop_duplicates(subset=['ts'], keep='last')
                      .sort_values('ts'))
            self._buf[sym] = cur[cur['ts'] >= lo_ms].reset_index(drop=True)
        keep = set(symbols)                               # 掉出票池的缓冲修剪防缓涨
        for s in [s for s in self._buf if s not in keep]:
            del self._buf[s]
        self.log('[label-feed] %d 币(冷%d 失败%d) %.1fs'
                 % (len(symbols), n_cold, n_fail, time.time() - t0))

    def labels(self, run_time):
        w1 = pd.Timestamp(run_time)
        w0 = w1 - pd.Timedelta(hours=12)
        out = {}
        for sym, df in self._buf.items():
            r = window_label(df, w0, w1)
            if r is not None:
                out[sym] = {'p12_cross1': r[0], 'p12_mae': r[1],
                            'p12_eff': p12_eff(r[0], r[1])}
        return out
