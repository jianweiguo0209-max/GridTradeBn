from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.gates import GridProposal, GateChain, SymbolLockGate
from gridtrade.execution.manager import GridManager
from gridtrade.execution.triggers import TriggerCondition, TriggerEngine, TriggerContext

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(store, price=100.0):
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=price)
    ex.set_price(BTC, price); ex.set_price(ETH, price)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    chain = GateChain([SymbolLockGate(gx.grids)])
    mgr = GridManager(gx, chain, stop_cfg=STOP_CFG)
    return ex, store, gx, mgr


def _proposal(symbol=BTC, tag='t0'):
    return GridProposal(exchange='fake', symbol=symbol, grid_params=dict(GP),
                        offset=0, tag=tag, source='test')


def test_run_monitor_cycle_syncs_then_reconciles_no_exit(store):
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(store, 100.0)
    ids = mgr.open_proposals([_proposal()])
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert set(out['reconciled'].keys()) == set(ids)
    assert out['reconciled'][ids[0]] == {'canceled': 0, 'replaced': 0}
    assert out['monitored'][0]['closed'] is False


def test_run_monitor_cycle_triggers_stop_close(store):
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(store, 100.0)
    ids = mgr.open_proposals([_proposal()])
    ex.set_price(BTC, 96.5)
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert out['monitored'][0]['closed'] is True
    assert gx.grids.get(ids[0]).status == 'CLOSED'


def test_restore_all_rebuilds_memory_then_monitor_works(store):
    from gridtrade.runtime.cycles import restore_all, run_monitor_cycle
    ex, store, gx, mgr = _setup(store, 100.0)
    ids = mgr.open_proposals([_proposal()])
    # 模拟「全新进程」：清空执行器内存态
    gx._geom.clear(); gx.live.clear(); gx._seq.clear()
    gx._trade_cursor.clear(); gx._funding_cursor.clear()
    restored = restore_all(Reconciler(gx))
    assert restored == ids
    # 重建后 monitor 周期不再 KeyError
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert out['monitored'][0]['closed'] is False


def test_restore_all_empty_when_no_active(store):
    from gridtrade.runtime.cycles import restore_all
    ex, store, gx, mgr = _setup(store)
    assert restore_all(Reconciler(gx)) == []


def test_monitor_cycle_syncs_fill_before_reconcile_no_phantom_replace(store):
    # 卖单成交后整轮 cycle：必须先 sync 摄入成交（把该单标 closed），再 reconcile。
    # 否则 reconcile 先跑、把已成交卖单当「被丢」重挂、覆盖成交 oid → 成交永不入账、净仓背离。
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(store, 100.0)
    gid = mgr.open_proposals([_proposal()])[0]
    ex.set_price(BTC, 100.6)        # 触发一个卖单成交（line5 卖 @100.4812）
    out = run_monitor_cycle(Reconciler(gx), mgr)
    # 成交已摄入：模型净仓须与交易所真实持仓一致
    model = gx.accounting.get(gid).net_position
    real = ex.fetch_positions(BTC).net_size
    assert abs(model - real) < 1e-9, f"position drift: model={model} real={real}"
    # 已成交的卖单不得被 reconcile 当「被丢」重挂
    assert out['reconciled'][gid]['replaced'] == 0


def test_monitor_cycle_reports_position_drift(store):
    # C：净仓对账接线——模型净仓与交易所真实持仓背离超容差时，cycle 收进 out['drift'] 并打日志。
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(store, 100.0)
    gid = mgr.open_proposals([_proposal()])[0]
    run_monitor_cycle(Reconciler(gx), mgr)        # 第一轮：模型与交易所一致
    order_num = gx._geom[gid]['order_num']
    # 外部成交（非网格 fill）动了交易所持仓 → sync 不会摄入、模型不变 → 背离活过 sync
    ex.create_market_order(BTC, 'sell', 3 * order_num, client_oid='external:0')
    logs = []
    out = run_monitor_cycle(Reconciler(gx), mgr, log=logs.append)
    assert gid in out['drift'] and out['drift'][gid]['ok'] is False
    assert any('position drift' in m for m in logs)


def test_monitor_cycle_grace_no_phantom_replace_then_ingests_late_fill(store):
    # E 端到端：单成交但成交本轮不可见（HL 延迟）→ reconcile 宽限不重挂、不覆盖 oid →
    # 下一轮成交可见 → sync 摄入、标 closed、不产生多余单。同一 reconciler 跨轮（grace 计数存活）。
    from gridtrade.runtime.cycles import run_monitor_cycle
    from gridtrade.exchanges.base import Trade
    ex, store, gx, mgr = _setup(store, 100.0)
    gid = mgr.open_proposals([_proposal()])[0]
    sell = [o for o in ex.fetch_open_orders(BTC) if o.side == 'sell'][0]
    rec = Reconciler(gx)                              # grace=2 默认
    ex._open.pop(sell.id, None)                       # 模拟"成交、从 book 消失，但成交尚不可见"
    out1 = run_monitor_cycle(rec, mgr)
    assert out1['reconciled'][gid]['replaced'] == 0   # 宽限：不重挂
    assert gx.orders.get(sell.client_oid).exchange_order_id == sell.id   # oid 未被覆盖
    # 成交变可见
    ex._trades.append(Trade(id='late', client_oid=sell.client_oid, symbol=BTC, side='sell',
                            price=sell.price, size=sell.size, fee=0.0, ts=1, order_id=sell.id))
    run_monitor_cycle(rec, mgr)
    assert any(f.trade_id == 'late' for f in gx.fills.list_by_grid(gid))   # 迟到成交已摄入
    assert gx.orders.get(sell.client_oid).status == 'closed'              # 该单标 closed，未被重挂


def test_monitor_cycle_lazy_restores_grid_opened_by_another_process(store):
    # 跨进程：scheduler 进程开网格（gx 内存有），monitor 进程（gx2 空内存、共享同 store/ex）
    # 直接 sync 会 KeyError；run_monitor_cycle 应先惰性 restore 再 monitor。
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(store, 100.0)
    mgr.open_proposals([_proposal()])
    gx2 = GridExecutor(ex, store, cap=1000.0, leverage=5.0)   # 新进程：空 _geom
    from gridtrade.execution.gates import GateChain, SymbolLockGate
    mgr2 = GridManager(gx2, GateChain([SymbolLockGate(gx2.grids)]), stop_cfg=STOP_CFG)
    out = run_monitor_cycle(Reconciler(gx2), mgr2)            # 不应 KeyError
    assert out['monitored'][0]['closed'] is False
    assert gx2.is_loaded(out['monitored'][0]['grid_id'])     # 已被惰性重建


class _FixedTrigger(TriggerCondition):
    def __init__(self, props):
        self._props = props
    def propose(self, ctx):
        return list(self._props)


def test_run_scheduler_cycle_closes_old_tag_then_opens_new(store):
    from gridtrade.runtime.cycles import run_scheduler_cycle
    import pandas as pd
    ex, store, gx, mgr = _setup(store, 100.0)
    old = mgr.open_proposals([_proposal(symbol=BTC, tag='t0')])   # 旧 BTC 网格 tag=t0
    engine = TriggerEngine([_FixedTrigger([_proposal(symbol=ETH, tag='t0')])])
    ctx = TriggerContext(exchange='fake', run_time=pd.Timestamp('2025-06-24 14:00:00'))
    out = run_scheduler_cycle(mgr, engine, Reconciler(gx), ctx, close_tag='t0')
    assert out['closed'] == old
    assert gx.grids.get(old[0]).status == 'CLOSED'
    assert len(out['opened']) == 1
    assert gx.grids.get(out['opened'][0]).symbol == ETH
    assert gx.grids.get(out['opened'][0]).status == 'ACTIVE'


def test_run_scheduler_cycle_restore_before_close_in_fresh_process(store):
    from gridtrade.runtime.cycles import run_scheduler_cycle
    import pandas as pd
    ex, store, gx, mgr = _setup(store, 100.0)
    old = mgr.open_proposals([_proposal(symbol=BTC, tag='t0')])
    # 模拟 scheduler scale-to-zero 全新进程：清空内存态
    gx._geom.clear(); gx.live.clear(); gx._seq.clear()
    gx._trade_cursor.clear(); gx._funding_cursor.clear()
    engine = TriggerEngine([])   # 不开新，只验证关旧前 restore 不 KeyError
    ctx = TriggerContext(exchange='fake', run_time=pd.Timestamp('2025-06-24 14:00:00'))
    out = run_scheduler_cycle(mgr, engine, Reconciler(gx), ctx, close_tag='t0')
    assert out['closed'] == old
    assert gx.grids.get(old[0]).status == 'CLOSED'


def test_run_scheduler_cycle_no_close_tag_only_opens(store):
    from gridtrade.runtime.cycles import run_scheduler_cycle
    import pandas as pd
    ex, store, gx, mgr = _setup(store, 100.0)
    engine = TriggerEngine([_FixedTrigger([_proposal(symbol=BTC, tag='t0')])])
    ctx = TriggerContext(exchange='fake', run_time=pd.Timestamp('2025-06-24 14:00:00'))
    out = run_scheduler_cycle(mgr, engine, Reconciler(gx), ctx)
    assert out['closed'] == []
    assert len(out['opened']) == 1


def test_monitor_cycle_resumes_stuck_closing_grid(store):
    # 模拟 close() 中途失败：网格停在 CLOSING、订单还挂、仓位还在。
    # monitor 循环应「续平」：撤单 + reduce + 落库 + 转 CLOSED（否则永远卡死、残仓无人认领）。
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(store, 100.0)
    gid = mgr.open_proposals([_proposal()])[0]
    ex.set_price(BTC, 98.5); gx.sync(gid, BTC)   # 真中性：驱动买线成交 → 累出净多
    g = gx.grids.get(gid)
    gx.grids.transition_status(gid, 'CLOSING', expected_version=g.version)  # 卡住
    assert ex.fetch_positions(BTC).net_size > 0
    run_monitor_cycle(Reconciler(gx), mgr)
    assert gx.grids.get(gid).status == 'CLOSED'                 # 续平到 CLOSED
    assert abs(ex.fetch_positions(BTC).net_size) <= gx.min_amount   # 仓位平了
    assert len(gx.records.list_by_grid(gid)) == 1              # 落了一条关仓记录


def test_finalize_close_does_not_duplicate_existing_record(store):
    # close 若曾落库但转 CLOSED 前失败，续平不得重复落库（幂等）。
    from gridtrade.state.models import Record
    ex, store, gx, mgr = _setup(store, 100.0)
    gid = mgr.open_proposals([_proposal()])[0]
    g = gx.grids.get(gid)
    gx.grids.transition_status(gid, 'CLOSING', expected_version=g.version)
    gx.records.add(Record(id='', grid_id=gid, exchange='fake', symbol=BTC,
                          exit_reason='prior'))   # 模拟已落一条
    gx.finalize_close(gid, BTC, '平仓恢复')
    assert len(gx.records.list_by_grid(gid)) == 1              # 不重复
    assert gx.grids.get(gid).status == 'CLOSED'


def test_monitor_cycle_logs_per_grid_degraded(store):
    # per-grid 故障必须打日志（否则故障在日志里隐形）。
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(store, 100.0)
    mgr.open_proposals([_proposal()])
    class _BadRec:
        ex = gx
        def restore(self, gid): pass
        def reconcile_open_orders(self, gid, sym, snapshot=None): raise RuntimeError('recon boom')
    logs = []
    run_monitor_cycle(_BadRec(), mgr, log=logs.append)
    assert any('recon boom' in s for s in logs)


def test_monitor_cycle_fails_stuck_opening_grid_with_no_orders(store):
    # 开仓首步即失败的死 OPENING（超时+零挂单）：自动判 FAILED（释放 symbol 槽）+ 结构化日志。
    # 护栏：未超时（正在开仓）或已有挂单（部分开仓，有清理负担）的 OPENING 一律不动。
    from gridtrade.runtime.cycles import run_monitor_cycle, STUCK_OPENING_TIMEOUT_SEC
    from gridtrade.state.models import Grid, GridOrder, OPENING, now_ms
    ex, store, gx, mgr = _setup(store, 100.0)
    old_ms = now_ms() - (STUCK_OPENING_TIMEOUT_SEC + 60) * 1000
    stuck = gx.grids.create(Grid(id='', exchange='fake', symbol=ETH, status=OPENING,
                                 tag='tS', created_at=old_ms, updated_at=old_ms))
    fresh = gx.grids.create(Grid(id='', exchange='fake', symbol=BTC, status=OPENING,
                                 tag='tF'))
    partial = gx.grids.create(Grid(id='', exchange='fake', symbol='SOL/USDT:USDT',
                                   status=OPENING, tag='tP',
                                   created_at=old_ms, updated_at=old_ms))
    gx.orders.upsert(GridOrder(client_oid='p1', grid_id=partial.id, line_index=0,
                               side='buy', price=99.0, size=1.0))
    logs = []
    run_monitor_cycle(Reconciler(gx), mgr, log=logs.append)
    assert gx.grids.get(stuck.id).status == 'FAILED'      # 超时+零挂单 → 自动 FAILED
    assert gx.grids.get(fresh.id).status == 'OPENING'     # 未超时 → 不动
    assert gx.grids.get(partial.id).status == 'OPENING'   # 有挂单 → 不动（不误杀部分开仓）
    assert any('stuck OPENING' in line for line in logs)  # 结构化日志（否则线上隐形）
