"""运行时循环体（design.md §8 两个 Fly Machine 角色的循环编排）。

数据源无关的纯编排：candle 数据由调用方放进 TriggerContext.symbol_candle_data。
while/sleep 守护进程、信号、心跳、config/币池/DataSource 接线属部署耦合（P4-deploy）。
"""
from typing import List

from gridtrade.state.models import ACTIVE


def _active_grids(grids_repo):
    return [g for g in grids_repo.list_active() if g.status == ACTIVE]


def restore_all(reconciler) -> List[str]:
    """重启自愈：为 DB 中所有 ACTIVE 网格重建执行器内存态。"""
    restored: List[str] = []
    for grid in _active_grids(reconciler.ex.grids):
        reconciler.restore(grid.id)
        restored.append(grid.id)
    return restored


def run_monitor_cycle(reconciler, manager) -> dict:
    """monitor 机循环体：先逐网格对账补单，再 monitor_all 止盈止损。"""
    reconciled = {}
    for grid in _active_grids(manager.executor.grids):
        reconciled[grid.id] = reconciler.reconcile_open_orders(grid.id, grid.symbol)
    monitored = manager.monitor_all()
    return {'reconciled': reconciled, 'monitored': monitored}
