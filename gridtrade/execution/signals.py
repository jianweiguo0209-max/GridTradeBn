"""实盘退出信号提供者：pv_spike（量能尖峰）+ funding_rate（HL 真实资金费率）。

设计要点：
- **legacy 满窗语义（2026-07-07）**：pv_spike 复用 core.grid_engine.calc_pv_spike（同一函数、
  同一 15min 粒度），数据取原生 15m K线 n+8 根（n=100 → 108 根 ≈ 27h），rolling(n) 为
  真滑动基线——对齐 legacy（OKX 时代 15m×rolling 满窗）语义；取数窗口与开格时刻解耦。
  （此前取「开网时刻→现在」1m，基线退化为开格以来 expanding，系移植漂移，已修。）
- **按 grid 节流缓存**：pv 是 15min 粒度、funding 是小时粒度，无需每 5s tick 打接口；
  每 grid 每 refresh_sec（默认 900s=15min）刷新一次，其余 tick 用缓存。
- **失败降级**：任一取数异常→返回该项安全默认（pv_spike=0 / funding_rate=0.0）+ 日志，
  不阻塞 sync/固定止损等其他退出判定。
"""
import time

from gridtrade.core.grid_engine import calc_pv_spike


class LiveSignalProvider:
    def __init__(self, adapter, *, mult=3, period='15min', n=233, refresh_sec=900,
                 now_fn=None, log=print):
        self.adapter = adapter
        self.mult = mult
        self.period = period
        self.n = n
        self.refresh_sec = float(refresh_sec)
        self._now = now_fn or time.time
        self.log = log
        self._cache = {}   # grid_id -> (fetched_at_sec, pv_spike, funding_rate)

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
            # legacy 满窗语义（spec 2026-07-07-pv-legacy-semantics-live）：原生 15m 取 n+8 根
            # （n=100→108 根≈27h），rolling(n) 为真滑动基线；窗口与 open_ms 解耦（open_ms 仅留签名兼容）。
            since_ms = now_ms - (self.n + 8) * 900_000
            bars = self.adapter.fetch_ohlcv(symbol, '15m', since_ms, now_ms)
            if bars is None or len(bars) == 0 or 'quote_volume' not in bars.columns:
                return 0
            sp = calc_pv_spike(bars, active_period=self.period, mult=self.mult, n=self.n)
            if sp is None or sp.empty:
                return 0
            return int(sp['pv_spike'].iloc[-1])
        except Exception as exc:     # 取数失败降级为「无尖峰」，不误触发也不阻塞
            self.log('[signals] pv_spike %s 失败降级: %r' % (symbol, exc))
            return 0

    def _funding_rate(self, symbol, now_ms):
        try:
            # 回看窗=结算周期+1h——币安 8h 结算下固定 3h 窗有 5/8 时间取不到最新费率(终审实证)。
            hours = float(getattr(self.adapter, 'FUNDING_INTERVAL_HOURS', 8)) + 1.0
            fh = self.adapter.fetch_funding_history(
                symbol, now_ms - int(hours * 3600_000), now_ms)
            if fh is None or len(fh) == 0:
                return 0.0
            return float(fh.sort_values('ts')['fundingRate'].iloc[-1])
        except Exception as exc:
            self.log('[signals] funding_rate %s 失败降级: %r' % (symbol, exc))
            return 0.0
