from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
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


def _setup(price=100.0):
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=price)
    ex.set_price(BTC, price); ex.set_price(ETH, price)
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    chain = GateChain([SymbolLockGate(gx.grids)])
    mgr = GridManager(gx, chain, stop_cfg=STOP_CFG)
    return ex, store, gx, mgr


def _proposal(symbol=BTC, tag='t0'):
    return GridProposal(exchange='fake', symbol=symbol, grid_params=dict(GP),
                        offset=0, tag=tag, source='test')


def test_run_monitor_cycle_reconciles_then_monitors_no_exit():
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(100.0)
    ids = mgr.open_proposals([_proposal()])
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert set(out['reconciled'].keys()) == set(ids)
    assert out['reconciled'][ids[0]] == {'canceled': 0, 'replaced': 0}
    assert out['monitored'][0]['closed'] is False


def test_run_monitor_cycle_triggers_stop_close():
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(100.0)
    ids = mgr.open_proposals([_proposal()])
    ex.set_price(BTC, 96.5)
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert out['monitored'][0]['closed'] is True
    assert gx.grids.get(ids[0]).status == 'CLOSED'


def test_restore_all_rebuilds_memory_then_monitor_works():
    from gridtrade.runtime.cycles import restore_all, run_monitor_cycle
    ex, store, gx, mgr = _setup(100.0)
    ids = mgr.open_proposals([_proposal()])
    # 模拟「全新进程」：清空执行器内存态
    gx._geom.clear(); gx.live.clear(); gx._seq.clear()
    gx._trade_cursor.clear(); gx._funding_cursor.clear()
    restored = restore_all(Reconciler(gx))
    assert restored == ids
    # 重建后 monitor 周期不再 KeyError
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert out['monitored'][0]['closed'] is False


def test_restore_all_empty_when_no_active():
    from gridtrade.runtime.cycles import restore_all
    ex, store, gx, mgr = _setup()
    assert restore_all(Reconciler(gx)) == []
