"""GridManager —— 组合编排器（design.md §6③）。

持有单个共享 GridExecutor（按 grid_id 管多网格，cap/leverage 共享 = legacy 均仓）、
准入门链 GateChain、可选事件总线。把「触发产出的提议 → 过门 → 开仓 → 发事件」与
「逐 ACTIVE 网格 monitor_grid → 平仓发事件」两段编排起来。
"""
from typing import List

from gridtrade.state.models import ACTIVE
from gridtrade.execution.events import GridOpened, GridClosed
from gridtrade.execution.monitor import monitor_grid


class GridManager:
    def __init__(self, executor, gate_chain, *, stop_cfg, margin_rate=0.05,
                 event_bus=None):
        self.executor = executor
        self.gates = gate_chain
        self.stop_cfg = stop_cfg
        self.margin_rate = float(margin_rate)
        self.bus = event_bus

    def _publish(self, event) -> None:
        if self.bus is not None:
            self.bus.publish(event)

    def open_proposals(self, proposals) -> List[str]:
        opened: List[str] = []
        for proposal in self.gates.filter(proposals):
            gid = self.executor.open(
                proposal.exchange, proposal.symbol, proposal.grid_params,
                offset=proposal.offset, tag=proposal.tag)
            opened.append(gid)
            self._publish(GridOpened(grid_id=gid, exchange=proposal.exchange,
                                     symbol=proposal.symbol, tag=proposal.tag))
        return opened

    def monitor_all(self) -> List[dict]:
        results: List[dict] = []
        # 取快照列表，只推进 ACTIVE 网格（PENDING/OPENING/CLOSING 为过渡态）
        active = [g for g in self.executor.grids.list_active()
                  if g.status == ACTIVE]
        for grid in active:
            res = monitor_grid(self.executor, grid.id, grid.symbol,
                               self.stop_cfg, margin_rate=self.margin_rate)
            if res['closed']:
                self._publish(GridClosed(
                    grid_id=grid.id, exchange=grid.exchange, symbol=grid.symbol,
                    reason=res['reason'], pnl_ratio=res['pnl_ratio']))
            results.append({'grid_id': grid.id, **res})
        return results
