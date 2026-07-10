from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.gates import GridProposal, GateChain
from gridtrade.execution.events import EventBus, GridOpened, GridClosed

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)],
                      price=price)
    ex.set_price(SYM, price)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    return ex, store, gx


def _proposal(symbol=SYM, exchange='fake'):
    return GridProposal(exchange=exchange, symbol=symbol, grid_params=dict(GP),
                        offset=0, tag='t0', source='test')


def _manager(gx, store, bus=None):
    from gridtrade.execution.manager import GridManager
    chain = GateChain([])
    return GridManager(gx, chain, stop_cfg=STOP_CFG, event_bus=bus)


def test_open_proposals_opens_passing_and_returns_ids(store):
    ex, store, gx = _setup(store)
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


def test_open_proposals_blocked_by_gate_not_opened(store):
    # cap=4（2026-07-11 换代）：同币第 4 格放行，第 5 格由 DB 槽位拒（SlotExhausted → 优雅跳过）。
    ex, store, gx = _setup(store)
    mgr = _manager(gx, store)
    for _ in range(3):
        mgr.open_proposals([_proposal()])        # 第 1-3 格
    assert len(mgr.open_proposals([_proposal()])) == 1   # 第 4 格：cap=4 放行
    ids5 = mgr.open_proposals([_proposal()])     # 第 5 格 -> 槽满跳过（不炸批）
    assert ids5 == []


def test_open_proposals_empty_list_noop(store):
    ex, store, gx = _setup(store)
    mgr = _manager(gx, store)
    assert mgr.open_proposals([]) == []


def test_monitor_all_no_exit_returns_open_results(store):
    ex, store, gx = _setup(store, 100.0)
    mgr = _manager(gx, store)
    mgr.open_proposals([_proposal()])
    res = mgr.monitor_all()
    assert len(res) == 1
    assert res[0]['closed'] is False and res[0]['reason'] is None


def test_monitor_all_triggers_stop_and_publishes_grid_closed(store):
    ex, store, gx = _setup(store, 100.0)
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


def test_monitor_all_no_active_grids_returns_empty(store):
    ex, store, gx = _setup(store)
    mgr = _manager(gx, store)
    assert mgr.monitor_all() == []


def test_close_by_tag_closes_matching_active_grids_and_publishes(store):
    ex, store, gx = _setup(store, 100.0)
    bus = EventBus(); closed_events = []
    bus.subscribe(lambda e: closed_events.append(e) if isinstance(e, GridClosed) else None)
    mgr = _manager(gx, store, bus)
    ids = mgr.open_proposals([_proposal()])          # tag='t0'
    out = mgr.close_by_tag('t0', '周期再平衡')
    assert out == ids
    assert gx.grids.get(ids[0]).status == 'CLOSED'
    assert len(closed_events) == 1
    assert closed_events[0].grid_id == ids[0] and closed_events[0].reason == '周期再平衡'


def test_close_by_tag_ignores_non_matching_tag(store):
    ex, store, gx = _setup(store, 100.0)
    mgr = _manager(gx, store)
    ids = mgr.open_proposals([_proposal()])          # tag='t0'
    out = mgr.close_by_tag('t999', '周期再平衡')      # 无匹配
    assert out == []
    assert gx.grids.get(ids[0]).status == 'ACTIVE'   # 未动


def test_monitor_all_publishes_orderfilled_per_new_fill(store):
    from gridtrade.execution.events import EventBus, OrderFilled
    ex, store, gx = _setup(store, 100.0)
    bus = EventBus(); filled = []
    bus.subscribe(lambda e: filled.append(e) if isinstance(e, OrderFilled) else None)
    mgr = _manager(gx, store, bus)
    mgr.open_proposals([_proposal()])
    ex.set_price(SYM, 100.6)              # 穿越上方一格 -> 成交
    out = mgr.monitor_all()
    # 事件数 == monitor_all 实报的本轮新成交数（不硬编码几何），且确有成交
    fills_reported = out[0]['fills']
    assert len(filled) == len(fills_reported) >= 1
    e = filled[0]
    assert e.symbol == SYM and e.side == 'sell' and e.size > 0 and e.fee > 0
    # 二次 monitor 无新成交 -> 不再发（幂等）
    filled.clear()
    mgr.monitor_all()
    assert filled == []


class _StubSignals:
    def __init__(self):
        self.evicted = []

    def get(self, grid_id, symbol, open_ms):
        return 0, 0.0

    def evict(self, grid_id):
        self.evicted.append(grid_id)


def _manager_with_signals(gx, sig, bus=None):
    from gridtrade.execution.manager import GridManager
    chain = GateChain([])
    return GridManager(gx, chain, stop_cfg=STOP_CFG, event_bus=bus, signal_provider=sig)


def test_monitor_all_evicts_signal_cache_on_close(store):
    ex, store, gx = _setup(store, 100.0)
    sig = _StubSignals()
    mgr = _manager_with_signals(gx, sig)
    ids = mgr.open_proposals([_proposal()])
    ex.set_price(SYM, 96.5)                  # 触发固定止损 -> 平仓
    mgr.monitor_all()
    assert sig.evicted == ids                # 平仓即清缓存


def test_close_by_tag_evicts_signal_cache(store):
    ex, store, gx = _setup(store, 100.0)
    sig = _StubSignals()
    mgr = _manager_with_signals(gx, sig)
    ids = mgr.open_proposals([_proposal()])
    mgr.close_by_tag('t0', '周期再平衡')
    assert sig.evicted == ids
