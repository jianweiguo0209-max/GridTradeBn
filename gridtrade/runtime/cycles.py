"""运行时循环体（design.md §8 两个 Fly Machine 角色的循环编排）。

数据源无关的纯编排：candle 数据由调用方放进 TriggerContext.symbol_candle_data。
while/sleep 守护进程、信号、心跳、config/币池/DataSource 接线属部署耦合（P4-deploy）。

per-grid 并行（parallel>1）：每个 ACTIVE 网格 = 一个单元（restore→信号→monitor→
reconcile 一气呵成）在 worker 线程跑，病态格只拖自己；主线程等待期间按 beat 回调
中途打点心跳（长轮不再假 stale）。执行器内存态按 grid_id 分键、跨格零共享；写
操作由 ResilientAdapter 全局写锁串行（HL nonce 约束）；事件发布收在主线程。
"""
import concurrent.futures as _cf
import time
from typing import List

from gridtrade.execution.events import GridClosed, OrderFilled
from gridtrade.execution.grid_executor import _TRADE_REFETCH_OVERLAP_MS
from gridtrade.execution.monitor import monitor_grid
from gridtrade.execution.snapshot import build_account_snapshot
from gridtrade.runtime.commands import consume_one
from gridtrade.state.models import ACTIVE, CLOSING, FAILED, OPENING, now_ms

# OPENING 正常在秒级完成（建行后立即批量挂单）；超过该时长仍零挂单 = 开仓首步即失败的
# 死网格（线上实证：testnet NEAR/gt06 卡 6h+，SymbolLockGate 因它锁死该币）。
STUCK_OPENING_TIMEOUT_SEC = 900

# CLOSING 续平宽限：健康关格（撤 ~40 挂单+平残仓）由发起方 1-2 分钟内完成；monitor
# 零等待抢续平会与发起进程（scheduler 轮换/monitor 止损）竞态——撤单/平仓/转态三段
# 撞车，每次轮换 4-6 条假 error（mainnet 2026-07-06 五次轮换全实证）。只救进入
# CLOSING 超过宽限仍未落定的真卡死网格（与 OPENING 超时守卫同构）。
STUCK_CLOSING_GRACE_SEC = 180.0


def _active_grids(grids_repo):
    return [g for g in grids_repo.list_active() if g.status == ACTIVE]


def _closing_grids(grids_repo):
    # close() 中途失败留下的 CLOSING 网格：撤单/reduce/落库/转 CLOSED 某步抛错卡住，需续平。
    return [g for g in grids_repo.list_active() if g.status == CLOSING]


def _opening_grids(grids_repo):
    # open() 首步失败留下的 OPENING 网格：建行后挂单全军覆没/进程死在挂单前，永不转 ACTIVE。
    return [g for g in grids_repo.list_active() if g.status == OPENING]


def restore_all(reconciler) -> List[str]:
    """重启自愈：为 DB 中所有 ACTIVE 网格重建执行器内存态。"""
    restored: List[str] = []
    for grid in _active_grids(reconciler.ex.grids):
        reconciler.restore(grid.id)
        restored.append(grid.id)
    return restored


def _grid_unit(reconciler, manager, grid, *, skip_replenish=False, snapshot=None) -> dict:
    """单网格监控单元（worker 线程执行体）：restore→信号→monitor→reconcile 一气呵成。

    **sync 必须在 reconcile 之前**：否则 reconcile 把刚成交、尚未入账的单当「被丢」
    重挂、覆盖成交 oid → 该成交永不入账、净仓往一边漂（线上 gt011 实证的核心缺陷）。
    该不变量本就是 per-grid 的，单元化后天然保持。sync 失败/触发平仓的网格本单元
    不 reconcile。

    只碰本网格的执行器内存态键（per-grid 分键，跨格零共享）。不发事件、不打日志——
    EventBus 与 log 由主线程统一处理，不进多线程。stage 记录故障阶段供主线程分类
    （restore/reconcile → degraded；monitor → monitored error，与旧两段式口径一致）。
    """
    ex = manager.executor
    t0 = time.monotonic()
    out = {'grid_id': grid.id, 'stage': 'restore'}
    try:
        if not ex.is_loaded(grid.id):     # 惰性 restore（他进程开的/本进程重启）
            reconciler.restore(grid.id)
        out['stage'] = 'monitor'
        pv_spike, funding_rate = 0, 0.0
        if manager.signals is not None:   # provider 内部已按 grid 节流+失败降级
            pv_spike, funding_rate = manager.signals.get(grid.id, grid.symbol,
                                                         grid.created_at)
        res = monitor_grid(ex, grid.id, grid.symbol, manager.stop_cfg,
                           margin_rate=manager.margin_rate,
                           skip_replenish=skip_replenish,
                           pv_spike=pv_spike, funding_rate=funding_rate,
                           snapshot=snapshot)
        out.update(res)
        if not res['closed']:
            out['stage'] = 'reconcile'
            out['reconciled'] = reconciler.reconcile_open_orders(grid.id, grid.symbol,
                                                                 snapshot=snapshot)
            d = reconciler.check_position_drift(grid.id, grid.symbol,
                                                snapshot=snapshot)   # C：净仓对账（只告警）
            if d is not None and not d['ok']:
                out['drift'] = d
            out['fuse'] = reconciler.reconcile_fuses(grid.id, grid.symbol,
                                                     snapshot=snapshot)   # 保险丝三态
    except Exception as exc:              # 降级：单元故障不掀翻整轮（绝不吞 BaseException）
        out['error'] = repr(exc)
    out['elapsed'] = time.monotonic() - t0
    return out


def run_monitor_cycle(reconciler, manager, log=print, *,
                      flags=None, commands=None, audit=None, exchange='',
                      equity_repo=None, snapshot_interval_sec=300,
                      beat=None, parallel=1, unit_warn_sec=30.0,
                      beat_every_sec=10.0,
                      closing_grace_sec=STUCK_CLOSING_GRACE_SEC) -> dict:
    """monitor 机循环体：逐网格隔离——单网格故障降级记录，不阻塞其他网格的对账/止损。

    顺序：① 续平卡死的 CLOSING 网格（超 closing_grace_sec 宽限才动手，幂等自愈；
    宽限内视为发起方仍在收尾，避免双进程抢关格竞态）①' 清死 OPENING（超时+零挂单→FAILED，
    释放 symbol 槽；有挂单/未超时不动）② 每个 ACTIVE 网格一个单元 _grid_unit
    （restore→sync→止损→reconcile），parallel>1 时线程池并发、=1 时原地串行（保底）。

    打点：beat 回调在长轮等待期间每 beat_every_sec 打一次心跳（假 stale 根治）；
    单元耗时超 unit_warn_sec 指名道姓；每轮打一行总结。
    per-grid 故障收进 degraded 并打日志（隔离防止一坏拖垮全轮，但绝不让故障在日志里隐形）。
    """
    ex = manager.executor
    reconciled = {}
    resumed: List[str] = []
    degraded = {}
    drift = {}
    t_round = time.monotonic()
    last_beat = [t_round]

    def _maybe_beat():
        if beat is None:
            return
        now = time.monotonic()
        if now - last_beat[0] >= beat_every_sec:
            last_beat[0] = now
            try:
                beat()
            except Exception as exc:      # 心跳失败降级，不影响本轮监控
                log('[monitor] mid-round beat failed: %r' % exc)

    for grid in _closing_grids(ex.grids):     # close() 中途失败留下的卡死网格 -> 续平
        try:
            age_s = (now_ms() - int(grid.updated_at or grid.created_at)) / 1000.0
            if age_s < closing_grace_sec:
                continue    # 关格进行中（发起方正在收尾）→ 勿抢；只救超宽限的真卡死
            if not ex.is_loaded(grid.id):
                reconciler.restore(grid.id)
            ex.finalize_close(grid.id, grid.symbol, '平仓恢复')
            resumed.append(grid.id)
        except Exception as exc:
            degraded[grid.id] = repr(exc)
    for grid in _opening_grids(ex.grids):     # 死 OPENING（超时+零挂单）-> FAILED（释放 symbol 槽）
        try:
            age_s = (now_ms() - int(grid.created_at)) / 1000.0
            if age_s < STUCK_OPENING_TIMEOUT_SEC or ex.orders.list_by_grid(grid.id):
                continue                      # 正在开仓 / 已有挂单（部分开仓有清理负担）→ 不动
            ex.grids.transition_status(grid.id, FAILED, expected_version=grid.version)
            log('[monitor] grid %s stuck OPENING -> FAILED (age=%ds, orders=0, %s tag=%s)'
                % (grid.id, age_s, grid.symbol, grid.tag))
        except Exception as exc:
            degraded[grid.id] = repr(exc)
    _maybe_beat()

    halted = bool(flags.get('trading_halted')) if flags is not None else False
    active = _active_grids(ex.grids)
    snapshot = None
    snap_failed = False
    if active:
        try:
            # 游标口径与逐格路径严格等价：trade 用 fills.max_ts（无成交=0，同旧行为；
            # 勿用 created_at 兜底——FakeExchange 等测试替身的成交 ts 是逻辑计数器，与
            # epoch 不可比较，且 HL since=0 也只回最近 2000 条，代价同现状）。
            # funding 游标读 DB（单元里的惰性 restore 尚未发生，不能依赖内存态），
            # created_at 兜底与 restore 语义一致。
            t_base, f_base = [], []
            for g in active:
                t_base.append(int(ex.fills.max_ts(g.id)))
                acc = ex.accounting.get(g.id)
                f_base.append(int(acc.funding_cursor) if (acc is not None and acc.funding_cursor)
                              else int(g.created_at))
            snapshot = build_account_snapshot(
                ex.adapter, sorted({g.symbol for g in active}),
                trade_since_ms=max(0, min(t_base) - _TRADE_REFETCH_OVERLAP_MS),
                funding_since_ms=max(0, min(f_base)))
        except Exception as exc:      # 整轮跳过（用户决定）：下轮重建；保险丝在交易所侧独立护网
            snap_failed = True
            log('[monitor] snapshot failed: %r (units skipped this round)' % exc)
    results: List[dict] = []
    if snap_failed:
        pass                          # 不派发任何单元；指令/equity/心跳照常
    elif parallel <= 1 or len(active) <= 1:   # 串行保底路径（MONITOR_PARALLEL=1 一键回退）
        for grid in active:
            results.append(_grid_unit(reconciler, manager, grid,
                                      skip_replenish=halted, snapshot=snapshot))
            _maybe_beat()
    else:
        next_slow_log = unit_warn_sec
        with _cf.ThreadPoolExecutor(max_workers=int(parallel)) as pool:
            pending = {pool.submit(_grid_unit, reconciler, manager, grid,
                                   skip_replenish=halted, snapshot=snapshot): grid
                       for grid in active}
            while pending:
                done, _ = _cf.wait(pending, timeout=1.0)
                _maybe_beat()
                for fut in done:
                    results.append(fut.result())
                    del pending[fut]
                waited = time.monotonic() - t_round
                if pending and waited >= next_slow_log:   # 在飞慢格可见（不杀线程，只指名）
                    next_slow_log += unit_warn_sec
                    log('[monitor] round slow: waiting on %s elapsed=%.1fs'
                        % (sorted(g.id for g in pending.values()), waited))

    by_grid = {g.id: g for g in active}
    for r in results:                         # 事件/归类收在主线程（EventBus 不进多线程）
        gid = r['grid_id']
        grid = by_grid[gid]
        err = r.get('error')
        if err is not None and r.get('stage') in ('restore', 'reconcile'):
            degraded[gid] = err
        if err is None or r.get('stage') == 'reconcile':   # monitor 段成功 → 事件照发
            for f in r.get('fills', []):
                manager._publish(OrderFilled(
                    grid_id=gid, symbol=grid.symbol, line_index=f['line_index'],
                    side=f['side'], price=f['price'], size=f['size'], fee=f['fee']))
            if r.get('closed'):
                if manager.signals is not None:
                    manager.signals.evict(gid)             # 平仓即清信号缓存
                manager._publish(GridClosed(
                    grid_id=gid, exchange=grid.exchange, symbol=grid.symbol,
                    reason=r['reason'], pnl_ratio=r['pnl_ratio']))
            if 'reconciled' in r:
                reconciled[gid] = r['reconciled']
            if 'drift' in r:
                drift[gid] = r['drift']
            fuse = r.get('fuse') or {}
            if fuse.get('fired'):
                log('[monitor] grid %s fuse fired -> grid closed' % gid)
            elif fuse.get('replaced'):
                # 健康网格几乎从不重挂；若每轮都打这行 = 保险丝没出现在 fetch_open_orders
                # （如 HL 触发单不在 frontendOpenOrders）→ 每轮重挂、孤儿触发单堆积，需排查。
                log('[monitor] grid %s fuse re-placed x%d' % (gid, fuse['replaced']))

    monitored = results
    for gid, err in degraded.items():         # per-grid 故障打日志（否则隐形）
        log('[monitor] grid %s degraded: %s' % (gid, err))
    for r in monitored:
        if 'error' in r and r.get('stage') == 'monitor':
            log('[monitor] grid %s monitor error: %s' % (r.get('grid_id'), r['error']))
    for gid, d in drift.items():              # 净仓背离打日志（不自动改仓，留人工/后续处置）
        log('[monitor] grid %s position drift: model=%s exchange=%s drift=%s tol=%s'
            % (gid, d['model'], d['exchange'], d['drift'], d['tol']))
    for r in monitored:                       # 慢格指名道姓（病态格从此在日志里可见）
        if r.get('elapsed', 0.0) > unit_warn_sec:
            log('[monitor] grid %s slow: %.1fs' % (r['grid_id'], r['elapsed']))
    if monitored:                             # 轮次总结行（巡检一眼看轮健康）
        slowest = max(monitored, key=lambda r: r.get('elapsed', 0.0))
        log('[monitor] round grids=%d ok=%d closed=%d degraded=%d elapsed=%.1fs slowest=%s:%.1fs'
            % (len(monitored),
               sum(1 for r in monitored if 'error' not in r),
               sum(1 for r in monitored if r.get('closed')),
               len(degraded), time.monotonic() - t_round,
               slowest['grid_id'], slowest.get('elapsed', 0.0)))

    if commands is not None and audit is not None:
        consume_one(commands, audit, manager, flags, exchange=exchange)
    if equity_repo is not None:
        try:
            bal = manager.executor.adapter.fetch_balance()
            equity_repo.add_if_due(bal.equity, getattr(bal, 'cash', None),
                                   interval_sec=int(snapshot_interval_sec))
        except Exception as exc:
            log('[monitor] equity snapshot skipped: %r' % exc)
    return {'reconciled': reconciled, 'resumed': resumed, 'degraded': degraded,
            'drift': drift, 'monitored': monitored}


def run_scheduler_cycle(manager, trigger_engine, reconciler, ctx, *,
                        close_tag=None, close_reason='周期再平衡',
                        open_enabled=True) -> dict:
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
    if not open_enabled:                      # MarketShockBrake:只关不开(spec 2026-07-08)
        return {'closed': closed, 'opened': [], 'shock_braked': True}
    proposals = trigger_engine.collect(ctx)
    opened = manager.open_proposals(proposals)
    return {'closed': closed, 'opened': opened}
