"""运行时循环体（design.md §8 两个 Fly Machine 角色的循环编排）。

数据源无关的纯编排：candle 数据由调用方放进 TriggerContext.symbol_candle_data。
while/sleep 守护进程、信号、心跳、config/币池/DataSource 接线属部署耦合（P4-deploy）。
"""
from typing import List

from gridtrade.state.models import ACTIVE, CLOSING


def _active_grids(grids_repo):
    return [g for g in grids_repo.list_active() if g.status == ACTIVE]


def _closing_grids(grids_repo):
    # close() 中途失败留下的 CLOSING 网格：撤单/reduce/落库/转 CLOSED 某步抛错卡住，需续平。
    return [g for g in grids_repo.list_active() if g.status == CLOSING]


def restore_all(reconciler) -> List[str]:
    """重启自愈：为 DB 中所有 ACTIVE 网格重建执行器内存态。"""
    restored: List[str] = []
    for grid in _active_grids(reconciler.ex.grids):
        reconciler.restore(grid.id)
        restored.append(grid.id)
    return restored


def run_monitor_cycle(reconciler, manager, log=print) -> dict:
    """monitor 机循环体：逐网格隔离——单网格故障降级记录，不阻塞其他网格的对账/止损。

    顺序：① 续平卡死的 CLOSING 网格（幂等自愈）② ACTIVE 网格对账 ③ monitor_all 止损/补单。
    per-grid 故障收进 degraded 并打日志（隔离防止一坏拖垮全轮，但绝不让故障在日志里隐形）。
    """
    ex = manager.executor
    reconciled = {}
    resumed: List[str] = []
    degraded = {}
    for grid in _closing_grids(ex.grids):     # close() 中途失败留下的卡死网格 -> 续平
        try:
            if not ex.is_loaded(grid.id):
                reconciler.restore(grid.id)
            ex.finalize_close(grid.id, grid.symbol, '平仓恢复')
            resumed.append(grid.id)
        except Exception as exc:
            degraded[grid.id] = repr(exc)
    for grid in _active_grids(ex.grids):
        try:
            if not ex.is_loaded(grid.id):
                reconciler.restore(grid.id)   # 他进程开的或本进程重启 -> 先重建几何/游标/记账
            reconciled[grid.id] = reconciler.reconcile_open_orders(grid.id, grid.symbol)
        except Exception as exc:              # 降级：坏网格不掀翻整轮（绝不吞 BaseException）
            degraded[grid.id] = repr(exc)
    monitored = manager.monitor_all()
    for gid, err in degraded.items():         # per-grid 故障打日志（否则隐形）
        log('[monitor] grid %s degraded: %s' % (gid, err))
    for r in monitored:
        if 'error' in r:
            log('[monitor] grid %s monitor error: %s' % (r.get('grid_id'), r['error']))
    return {'reconciled': reconciled, 'resumed': resumed,
            'degraded': degraded, 'monitored': monitored}


def run_scheduler_cycle(manager, trigger_engine, reconciler, ctx, *,
                        close_tag=None, close_reason='周期再平衡') -> dict:
    """scheduler 机循环体（复刻 legacy 主流程顺序）：先关旧 tag 网格、再触发→准入→开仓。

    scheduler 机 scale-to-zero（全新进程），关旧前先 Reconciler.restore 重建内存态，
    否则 executor.close 取不到 _geom/live。
    """
    closed: List[str] = []
    if close_tag is not None:
        to_close = [g for g in _active_grids(manager.executor.grids)
                    if g.tag == close_tag]
        for grid in to_close:
            reconciler.restore(grid.id)   # 全新进程：先重建内存态
        closed = manager.close_by_tag(close_tag, close_reason)
    proposals = trigger_engine.collect(ctx)
    opened = manager.open_proposals(proposals)
    return {'closed': closed, 'opened': opened}
