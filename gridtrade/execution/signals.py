"""实盘退出信号提供者：pv_spike（量能尖峰）+ funding_rate（真实资金费率）。

设计要点：
- **与回测同源（方案C，2026-07-18）**：pv_spike 复用 core.grid_engine.calc_pv_spike —— 现在真的
  同源了。该函数算「**截至 now 的滚动 period 窗**」，故这里必须喂**原生 1m**、取 n+8 个 period
  （n=100 → 1620 根 ≈27h）供 rolling 基线；窗口与开格时刻解耦。
  修正史：①最初取「开网→现在」1m，基线退化为 expanding（移植漂移，07-07 修）；②改取原生 15m 后，
  calc_pv_spike 的 resample 成空操作、`iloc[-1]` 取到**进行中的半截桶**，而回测那侧是**整桶
  （含未来）**广播 —— 两侧口径从未对账过（回测 67.2% 的格窗见尖峰 vs 实盘 20.6%，丢 69%）。
  07-15 那次只对齐了 rolling 基线（n+8 根前置历史），**评估点没对齐**。方案C 两侧统一为滚动窗。
- **按 grid 节流缓存**：每 grid 每 refresh_sec（默认 900s=15min）刷新一次，其余 tick 用缓存。
  滚动窗宽 = period，信号在尖峰后粘住整整一个 period → refresh_sec ≤ period 时**必能命中**，
  旧口径的相位锁（scheduler 整点唤醒使采样卡在桶内第 1-7 分钟、命中率 0.16%）已消失。
  残留：回测逐 1m 判、实盘每 refresh_sec 判 → 实盘可能晚至多 refresh_sec 才动作（已知、二阶）。
- **失败降级**：任一取数异常→返回该项安全默认（pv_spike=0 / funding_rate=0.0）+ 日志，
  不阻塞 sync/固定止损等其他退出判定。
"""
import time

import pandas as pd

from gridtrade.core.grid_engine import calc_pv_spike
from gridtrade.execution.bar_buffer import OneMinuteBarBuffer


def _period_ms(period):
    """'15min' → 900_000。此前硬编码 900_000，period 可配时会悄悄失配。"""
    return int(pd.Timedelta(period).total_seconds() * 1000)


class LiveSignalProvider:
    def __init__(self, adapter, *, mult=3, period='15min', n=233, refresh_sec=60,
                 now_fn=None, log=print):
        self.adapter = adapter
        self.mult = mult
        self.period = period
        self.n = n
        self.refresh_sec = float(refresh_sec)
        self._now = now_fn or time.time
        self.log = log
        self._cache = {}   # grid_id -> (fetched_at_sec, pv_spike, funding_rate)
        # symbol 级费率缓存（2026-07-23）：cap2 同币双格下按 grid 节流会同币重复取数
        # （日志实锤 GWEI 同秒×2）。费率每 8h 才结算，refresh_sec 内复用零信息损失。
        self._fr_cache = {}   # symbol -> (fetched_at_sec, funding_rate)
        # per-symbol 已收盘 1m 滚动缓冲：冷载全窗、之后增量、失败沿用缓冲、丢 forming 桶(见 bar_buffer)
        self._buffer = OneMinuteBarBuffer(
            fetch_fn=lambda sym, s, u: adapter.fetch_ohlcv(sym, '1m', s, u),
            window_ms=(self.n + 8) * _period_ms(self.period),
            now_fn=self._now, log=log)

    def get(self, grid_id, symbol, open_ms):
        """返回 (pv_spike:int(0/1), funding_rate:float)。节流：refresh_sec 内复用缓存。"""
        now = self._now()
        c = self._cache.get(grid_id)
        if c is not None and (now - c[0]) < self.refresh_sec:
            return c[1], c[2]
        now_ms = int(now * 1000)
        pv = self._pv_spike(symbol, int(open_ms), now_ms)
        fr = self._funding_rate(symbol, now_ms)
        self._cache[grid_id] = (now, pv, fr)
        return pv, fr

    def evict(self, grid_id):
        """网格平仓后清掉其缓存条目，避免已平网格在缓存里无限累积。缺失也安全。"""
        self._cache.pop(grid_id, None)

    def _pv_spike(self, symbol, open_ms, now_ms):
        try:
            # 从缓冲取**已收盘 1m**（丢 forming 半截桶=(b)；增量取数=治(c)降级；窗宽=n+8个period）。
            # 粒度 1m + 收盘桶 + 逐分钟评估 → 与回测 pv_spike_for_window/calc_pv_spike 机制对齐。
            bars = self._buffer.get_closed_bars(symbol)
            if bars is None or bars.empty or 'quote_volume' not in bars.columns:
                return 0
            sp = calc_pv_spike(bars, active_period=self.period, mult=self.mult, n=self.n)
            if sp is None or sp.empty:
                return 0
            return int(sp['pv_spike'].iloc[-1])
        except Exception as exc:     # 取数失败降级为「无尖峰」，不误触发也不阻塞
            self.log('[signals] pv_spike %s 失败降级: %r' % (symbol, exc))
            return 0

    def _funding_rate(self, symbol, now_ms):
        now = now_ms / 1000.0
        c = self._fr_cache.get(symbol)
        if c is not None and (now - c[0]) < self.refresh_sec:
            return c[1]
        try:
            # 回看窗=结算周期+1h——币安 8h 结算下固定 3h 窗有 5/8 时间取不到最新费率(终审实证)。
            hours = float(getattr(self.adapter, 'FUNDING_INTERVAL_HOURS', 8)) + 1.0
            fh = self.adapter.fetch_funding_history(
                symbol, now_ms - int(hours * 3600_000), now_ms)
            if fh is None or len(fh) == 0:
                return 0.0            # 空结果（如新上币）不缓存：下次照常重试
            fr = float(fh.sort_values('ts')['fundingRate'].iloc[-1])
            self._fr_cache[symbol] = (now, fr)   # 只缓存真取到的费率；失败/空不污染
            return fr
        except Exception as exc:
            self.log('[signals] funding_rate %s 失败降级: %r' % (symbol, exc))
            return 0.0
