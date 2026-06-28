from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.gates import GridProposal, GateChain, SymbolLockGate
from gridtrade.execution.events import EventBus, GridOpened, GridClosed

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)],
                      price=price)
    ex.set_price(SYM, price)
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    return ex, store, gx


def _proposal(symbol=SYM, exchange='fake'):
    return GridProposal(exchange=exchange, symbol=symbol, grid_params=dict(GP),
                        offset=0, tag='t0', source='test')


def _manager(gx, store, bus=None):
    from gridtrade.execution.manager import GridManager
    chain = GateChain([SymbolLockGate(gx.grids)])
    return GridManager(gx, chain, stop_cfg=STOP_CFG, event_bus=bus)


def test_open_proposals_opens_passing_and_returns_ids():
    ex, store, gx = _setup()
    bus = EventBus(); opened_events = []
    bus.subscribe(lambda e: opened_events.append(e) if isinstance(e, GridOpened) else None)
    mgr = _manager(gx, store, bus)
    ids = mgr.open_proposals([_proposal()])
    assert len(ids) == 1
    assert gx.grids.get(ids[0]).status == 'ACTIVE'
    # 发了 GridOpened 事件，字段正确
    assert len(opened_events) == 1
    assert opened_events[0].grid_id == ids[0] and opened_events[0].symbol == SYM
    assert opened_events[0].tag == 't0'


def test_open_proposals_blocked_by_gate_not_opened():
    ex, store, gx = _setup()
    mgr = _manager(gx, store)
    mgr.open_proposals([_proposal()])           # 先开一个 BTC 活跃网格
    ids2 = mgr.open_proposals([_proposal()])     # 同币种再提议 -> SymbolLockGate 拦
    assert ids2 == []


def test_open_proposals_empty_list_noop():
    ex, store, gx = _setup()
    mgr = _manager(gx, store)
    assert mgr.open_proposals([]) == []


def test_monitor_all_no_exit_returns_open_results():
    ex, store, gx = _setup(100.0)
    mgr = _manager(gx, store)
    mgr.open_proposals([_proposal()])
    res = mgr.monitor_all()
    assert len(res) == 1
    assert res[0]['closed'] is False and res[0]['reason'] is None


def test_monitor_all_triggers_stop_and_publishes_grid_closed():
    ex, store, gx = _setup(100.0)
    bus = EventBus(); closed_events = []
    bus.subscribe(lambda e: closed_events.append(e) if isinstance(e, GridClosed) else None)
    mgr = _manager(gx, store, bus)
    ids = mgr.open_proposals([_proposal()])
    ex.set_price(SYM, 96.5)   # 大跌触发固定止损
    res = mgr.monitor_all()
    assert res[0]['closed'] is True and res[0]['reason'] == '固定止损'
    assert gx.grids.get(ids[0]).status == 'CLOSED'
    # 发了 GridClosed 事件
    assert len(closed_events) == 1
    assert closed_events[0].grid_id == ids[0] and closed_events[0].reason == '固定止损'


def test_monitor_all_no_active_grids_returns_empty():
    ex, store, gx = _setup()
    mgr = _manager(gx, store)
    assert mgr.monitor_all() == []
