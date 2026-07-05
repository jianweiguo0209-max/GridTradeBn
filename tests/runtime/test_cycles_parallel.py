# tests/runtime/test_cycles_parallel.py —— per-grid 并行监控 + 长轮中途打点
import threading
import time

import ccxt

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.gates import GridProposal, GateChain, SymbolLockGate
from gridtrade.execution.manager import GridManager
from gridtrade.runtime.cycles import run_monitor_cycle

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'
SOL = 'SOL/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(store, price=100.0, symbols=(BTC, ETH, SOL)):
    insts = [Instrument(s, 0.1, 0.001, 0.001, 'live', 0) for s in symbols]
    ex = FakeExchange(instruments=insts, price=price)
    for s in symbols:
        ex.set_price(s, price)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    chain = GateChain([SymbolLockGate(gx.grids)])
    mgr = GridManager(gx, chain, stop_cfg=STOP_CFG)
    return ex, gx, mgr


def _proposal(symbol, tag='t0'):
    return GridProposal(exchange='fake', symbol=symbol, grid_params=dict(GP),
                        offset=0, tag=tag, source='test')


class _Wrapped:
    """FakeExchange 委托包装：可对指定 symbol 的 fetch_my_trades 注入延迟/异常。"""
    def __init__(self, inner, *, slow_symbol=None, slow_sec=0.0,
                 fail_symbol=None, fail_exc=None):
        self._inner = inner
        self._slow_symbol = slow_symbol
        self._slow_sec = slow_sec
        self._fail_symbol = fail_symbol
        self._fail_exc = fail_exc

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def fetch_my_trades(self, symbol, since_ms=None):
        if symbol == self._slow_symbol and self._slow_sec:
            time.sleep(self._slow_sec)
        if symbol == self._fail_symbol and self._fail_exc is not None:
            raise self._fail_exc
        return self._inner.fetch_my_trades(symbol, since_ms=since_ms)


def test_parallel_matches_serial_invariants(store):
    # 并行(4) 跑三网格：结果集合与串行不变量一致（全 reconciled、无 close、无 drift）。
    ex, gx, mgr = _setup(store)
    ids = mgr.open_proposals([_proposal(BTC, 't0'), _proposal(ETH, 't1'),
                              _proposal(SOL, 't2')])
    out = run_monitor_cycle(Reconciler(gx), mgr, parallel=4)
    assert set(out['reconciled'].keys()) == set(ids)
    assert all(out['reconciled'][g] == {'canceled': 0, 'replaced': 0} for g in ids)
    assert {r['grid_id'] for r in out['monitored']} == set(ids)
    assert all(r['closed'] is False for r in out['monitored'])
    assert out['degraded'] == {} and out['drift'] == {}


def test_parallel_ingests_fill_before_reconcile_no_phantom_replace(store):
    # 核心不变量在并行下保持：成交先摄入再对账，不幻影重挂、净仓与交易所一致。
    ex, gx, mgr = _setup(store)
    gid = mgr.open_proposals([_proposal(BTC)])[0]
    mgr.open_proposals([_proposal(ETH, 't1')])
    ex.set_price(BTC, 100.6)          # 触发 BTC 一个卖单成交
    out = run_monitor_cycle(Reconciler(gx), mgr, parallel=4)
    model = gx.accounting.get(gid).net_position
    real = ex.fetch_positions(BTC).net_size
    assert abs(model - real) < 1e-9
    assert out['reconciled'][gid]['replaced'] == 0


def test_parallel_stop_close_works(store):
    ex, gx, mgr = _setup(store)
    ids = mgr.open_proposals([_proposal(BTC), _proposal(ETH, 't1')])
    ex.set_price(BTC, 96.5)           # BTC 触发止损
    out = run_monitor_cycle(Reconciler(gx), mgr, parallel=4)
    by_gid = {r['grid_id']: r for r in out['monitored']}
    assert by_gid[ids[0]]['closed'] is True
    assert gx.grids.get(ids[0]).status == 'CLOSED'
    assert by_gid[ids[1]]['closed'] is False           # ETH 不受影响
    assert ids[1] in out['reconciled'] and ids[0] not in out['reconciled']


def test_parallel_one_grid_write_error_isolated(store):
    # 写路径（补单）逐格隔离：ETH 补单被拒 → 该格 monitored error，BTC 照常。
    # （读路径已快照化，读故障=快照失败整轮跳过，见 test_cycles_snapshot / test_chaos_cycle）
    class _FailOrder:
        def __init__(self, inner, fail_symbol):
            self._inner = inner
            self._fail_symbol = fail_symbol
        def __getattr__(self, name):
            return getattr(self._inner, name)
        def create_limit_order(self, symbol, side, price, size, **kw):
            if symbol == self._fail_symbol:
                raise ccxt.ExchangeError('boom')
            return self._inner.create_limit_order(symbol, side, price, size, **kw)

    ex, gx, mgr = _setup(store)
    ids = mgr.open_proposals([_proposal(BTC), _proposal(ETH, 't1')])
    rec = Reconciler(gx)
    sell_eth = [o for o in ex.fetch_open_orders(ETH) if o.side == 'sell'][0]
    ex._open.pop(sell_eth.id, None)   # ETH 丢单（成交不可见）→ E2 宽限 2 轮后重挂
    gx.adapter = _FailOrder(ex, ETH)  # ETH 的一切下限价单被拒
    run_monitor_cycle(rec, mgr, parallel=4)          # 宽限第 1 轮
    logs = []
    out = run_monitor_cycle(rec, mgr, parallel=4, log=logs.append)   # 第 2 轮重挂 → 被拒
    assert ids[1] in out['degraded'] and 'boom' in out['degraded'][ids[1]]
    assert ids[0] in out['reconciled'] and ids[1] not in out['reconciled']
    by_gid = {r['grid_id']: r for r in out['monitored']}
    assert by_gid[ids[0]]['closed'] is False
    assert any('degraded' in s and 'boom' in s for s in logs)


def test_beat_called_during_long_round_parallel_and_serial(store):
    # 长轮中途打点：等待期间 beat 被调用（beat_every_sec=0 → 每次机会都打）。
    for par in (4, 1):
        beats = []
        ex, gx, mgr = _setup(store)
        mgr.open_proposals([_proposal(BTC), _proposal(ETH, 't1')])
        gx.adapter = _Wrapped(ex, slow_symbol=BTC, slow_sec=0.05)
        run_monitor_cycle(Reconciler(gx), mgr, parallel=par,
                          beat=lambda: beats.append(1), beat_every_sec=0.0)
        assert beats, 'parallel=%d 未打点' % par
        # 清场给下一轮参数复用（两个网格关掉，避免 SymbolLock 影响）
        for g in list(gx.grids.list_active()):
            if g.status == 'ACTIVE':
                gx.close(g.id, g.symbol, 'test cleanup')


def test_beat_failure_does_not_break_cycle(store):
    ex, gx, mgr = _setup(store)
    ids = mgr.open_proposals([_proposal(BTC)])
    def _bad_beat():
        raise RuntimeError('hb down')
    logs = []
    out = run_monitor_cycle(Reconciler(gx), mgr, parallel=1,
                            beat=_bad_beat, beat_every_sec=0.0, log=logs.append)
    assert set(out['reconciled'].keys()) == set(ids)     # 监控照常
    assert any('beat failed' in s for s in logs)


def test_slow_unit_and_round_summary_logged(store):
    ex, gx, mgr = _setup(store)
    mgr.open_proposals([_proposal(BTC)])
    gx.adapter = _Wrapped(ex, slow_symbol=BTC, slow_sec=0.02)
    logs = []
    run_monitor_cycle(Reconciler(gx), mgr, parallel=4, unit_warn_sec=0.01,
                      log=logs.append)
    assert any('slow' in s and 'grid' in s for s in logs)          # 慢格指名道姓
    assert any(s.startswith('[monitor] round grids=1') for s in logs)  # 轮次总结行


def test_parallel_publishes_events_from_main_thread(store):
    # 事件仍然发布（收在主线程）：成交 → OrderFilled；止损 → GridClosed。
    from gridtrade.execution.events import EventBus, GridClosed, OrderFilled
    ex, gx, mgr = _setup(store)
    bus = EventBus()
    got = []
    bus.subscribe(got.append)
    mgr.bus = bus
    ids = mgr.open_proposals([_proposal(BTC), _proposal(ETH, 't1')])
    ex.set_price(BTC, 100.6)          # BTC 成交
    ex.set_price(ETH, 96.5)           # ETH 止损
    run_monitor_cycle(Reconciler(gx), mgr, parallel=4)
    kinds = [type(e).__name__ for e in got]
    assert 'OrderFilled' in kinds and 'GridClosed' in kinds
    closed = [e for e in got if isinstance(e, GridClosed)]
    assert closed and closed[0].grid_id == ids[1]
