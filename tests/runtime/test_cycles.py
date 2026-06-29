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


def test_run_monitor_cycle_reconciles_then_monitors_no_exit(store):
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
        def reconcile_open_orders(self, gid, sym): raise RuntimeError('recon boom')
    logs = []
    run_monitor_cycle(_BadRec(), mgr, log=logs.append)
    assert any('recon boom' in s for s in logs)
