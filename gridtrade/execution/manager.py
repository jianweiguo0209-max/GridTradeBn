"""GridManager —— 组合编排器（design.md §6③）。

持有单个共享 GridExecutor（按 grid_id 管多网格，cap/leverage 共享 = legacy 均仓）、
准入门链 GateChain、可选事件总线。把「触发产出的提议 → 过门 → 开仓 → 发事件」与
「逐 ACTIVE 网格 monitor_grid → 平仓发事件」两段编排起来。
"""
from typing import List

from gridtrade.state.models import ACTIVE, PENDING, OPENING, FAILED, SlotExhausted
from gridtrade.execution.events import GridOpened, GridClosed, OrderFilled
from gridtrade.execution.monitor import monitor_grid


class GridManager:
    def __init__(self, executor, gate_chain, *, stop_cfg, margin_rate=0.05,
                 event_bus=None, signal_provider=None):
        self.executor = executor
        self.gates = gate_chain
        self.stop_cfg = stop_cfg
        self.margin_rate = float(margin_rate)
        self.bus = event_bus
        self.signals = signal_provider   # None=不算 pv/funding（退化为仅固定止损/回撤/爆仓）

    def _publish(self, event) -> None:
        if self.bus is not None:
            self.bus.publish(event)

    def open_proposals(self, proposals) -> List[str]:
        opened: List[str] = []
        for proposal in self.gates.filter(proposals):
            try:
                # cap=proposal.cap：门链定稿 cap（FuseCoverageGate 降档护全额，spec 2026-07-15 §五）。
                # None=未干预 → executor 回退自己的动态 cap（原行为）。不传即降档失效（评审实测）。
                gid = self.executor.open(
                    proposal.exchange, proposal.symbol, proposal.grid_params,
                    offset=proposal.offset, tag=proposal.tag, cap=proposal.cap)
            except SlotExhausted as exc:
                # 同币并发 cap 的唯一裁决层=DB 槽位（SymbolLockGate 已删，spec
                # 2026-07-06-tiered-*）：逐提议隔离——跳过本提议、其余照开；
                # 可观测性沿用 [gate] 口径（该开未开必须留痕）。
                print('[gate] rejected %s tag=%s by SlotCap: %s'
                      % (proposal.symbol, proposal.tag, exc), flush=True)
                continue
            except Exception as exc:
                # 逐提议隔离（与 monitor 逐格隔离同构；testnet 05:00 -2027 实证：此前只
                # catch SlotExhausted，交易所拒单冒泡致整轮 degraded、该 offset 空 12h 到下次
                # 轮换）：记录 + 清半开格（撤本格挂单 + 转 FAILED 释放槽位）+ 其余提议照开。
                # 合并注（fuse-coverage-guard 2026-07-15）：fuse 建网失败（降档后 cap 太小 →
                # RuntimeError）本另设 except RuntimeError 隔离；此 catch-all 是其超集且额外清
                # 半开格，故收编于此，不再单列。
                print('[open] rejected %s tag=%s: %r —— 清半开格、隔离续开'
                      % (proposal.symbol, proposal.tag, exc), flush=True)
                self._fail_half_open(proposal.exchange, proposal.symbol)
                continue
            opened.append(gid)
            self._publish(GridOpened(grid_id=gid, exchange=proposal.exchange,
                                     symbol=proposal.symbol, tag=proposal.tag))
        return opened

    def _fail_half_open(self, exchange, symbol) -> None:
        """开格中途失败留下的 PENDING/OPENING 格：逐单撤（不 cancel_all，防伤同币兄弟活跃格）
        + 转 FAILED 释放槽位。撤单/转态各自 try：清理尽力而为，绝不二次抛掀翻整轮。"""
        ex = self.executor
        for g in ex.grids.list_active():
            if g.exchange != exchange or g.symbol != symbol or g.status not in (PENDING, OPENING):
                continue
            for o in ex.orders.list_open_by_grid(g.id):
                if getattr(o, 'exchange_order_id', None):
                    try:
                        ex.adapter.cancel_order(symbol, o.exchange_order_id)
                    except Exception:
                        pass
            try:
                ex.grids.transition_status(g.id, FAILED, expected_version=g.version)
            except Exception:
                pass

    def monitor_all(self, skip_replenish=False) -> List[dict]:
        results: List[dict] = []
        # 取快照列表，只推进 ACTIVE 网格（PENDING/OPENING/CLOSING 为过渡态）
        active = [g for g in self.executor.grids.list_active()
                  if g.status == ACTIVE]
        for grid in active:
            try:
                pv_spike, pv_dir, funding_rate = 0, 0, 0.0
                if self.signals is not None:   # 算 pv_spike/funding（provider 内部已按 grid 节流+失败降级）
                    pv_spike, pv_dir, funding_rate = self.signals.get(grid.id, grid.symbol, grid.created_at)
                res = monitor_grid(self.executor, grid.id, grid.symbol,
                                   self.stop_cfg, margin_rate=self.margin_rate,
                                   skip_replenish=skip_replenish,
                                   pv_spike=pv_spike, pv_dir=pv_dir, funding_rate=funding_rate)
            except Exception as exc:   # 单网格 monitor 故障降级，不阻塞其他网格的止损/记账
                results.append({'grid_id': grid.id, 'error': repr(exc)})
                continue
            for f in res.get('fills', []):
                self._publish(OrderFilled(
                    grid_id=grid.id, symbol=grid.symbol, line_index=f['line_index'],
                    side=f['side'], price=f['price'], size=f['size'], fee=f['fee']))
            if res['closed']:
                if self.signals is not None:
                    self.signals.evict(grid.id)      # 平仓即清信号缓存
                self._publish(GridClosed(
                    grid_id=grid.id, exchange=grid.exchange, symbol=grid.symbol,
                    reason=res['reason'], pnl_ratio=res['pnl_ratio']))
            results.append({'grid_id': grid.id, **res})
        return results

    def close_by_tag(self, tag: str, reason: str,
                     exclude_symbols=frozenset()) -> List[str]:
        # 按币分组走 close_set(spec 2026-07-11-symbol-desk):同 tag 同币多格(罕见)
        # 净额化一次出清;单格集合退化 ≡ 旧逐格路径。exclude_symbols=外部干预熔断币
        # (spec 2026-07-12 组件三):关格是交易所写入,熔断中不动、留 ACTIVE 待 resolve。
        closed: List[str] = []
        active = [g for g in self.executor.grids.list_active()
                  if g.status == ACTIVE and g.tag == tag
                  and g.symbol not in exclude_symbols]
        by_sym = {}
        for g in active:
            by_sym.setdefault(g.symbol, []).append(g)
        for symbol, grids in sorted(by_sym.items()):
            results = self.executor.ledger.close_set([g.id for g in grids],
                                                     symbol, reason)
            g_by_id = {g.id: g for g in grids}
            for res in results:
                gid = res['grid_id']
                grid = g_by_id[gid]
                if self.signals is not None:
                    self.signals.evict(gid)          # 平仓即清信号缓存
                self._publish(GridClosed(
                    grid_id=gid, exchange=grid.exchange, symbol=symbol,
                    reason=res['reason'], pnl_ratio=res['pnl_ratio']))
                closed.append(gid)
        return closed
