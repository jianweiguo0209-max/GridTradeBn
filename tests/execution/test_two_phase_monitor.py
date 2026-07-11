# tests/execution/test_two_phase_monitor.py
"""组件二编排(spec 2026-07-11-symbol-desk):monitor 两阶段。
阶段 A 并行只读决策(defer_close 出意向不执行);阶段 B 同币意向合并一次 close_set
(PV 币级信号下 N 格同触发 → 恰 1 张净额市价单),跨币并行不互锁。
defer_close 默认 False ⇒ 既有直调路径零改动。"""
import pandas as pd

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.manager import GridManager
from gridtrade.execution.monitor import monitor_grid
from gridtrade.execution.reconciler import Reconciler
from gridtrade.runtime.cycles import run_monitor_cycle

AAA = 'AAA/USDT:USDT'
BBB = 'BBB/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP = {'stop_loss': 0.034, 'trailing_k': 0.15, 'trailing_floor': 0.015,
        'pv_pnl_thr': 0.005, 'pv_mult': 3, 'pv_period': '15min', 'pv_n': 100}


def _setup(store, symbols):
    ex = FakeExchange(instruments=[Instrument(s, 0.1, 0.001, 0.001, 'live', 0)
                                   for s in symbols], price=100.0)
    for s in symbols:
        ex.set_price(s, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    mgr = GridManager(gx, None, stop_cfg=STOP)
    rec = Reconciler(gx)
    return ex, gx, mgr, rec


def _force_long(ex, gx, gid, sym, qty):
    gx.live[gid].record_fill(100.0, 'buy', qty, 1000)
    cur = ex.fetch_positions(sym).net_size
    ex._pos[sym] = type(ex.fetch_positions(sym))(sym, cur + qty, 100.0)


def test_defer_close_returns_intent_without_executing(store):
    ex, gx, mgr, rec = _setup(store, [AAA])
    ga = gx.open('fake', AAA, dict(GP))
    _force_long(ex, gx, ga, AAA, 5.0)
    ex.set_price(AAA, 90.0)                    # 浮亏 -5% < -3.4% → 固定止损
    before = len(ex._trades)
    out = monitor_grid(gx, ga, AAA, STOP, defer_close=True)
    assert out['close_intent'] == '固定止损' and out['closed'] is False
    assert len(ex._trades) == before           # 阶段 A 零执行
    assert gx.grids.get(ga).status == 'ACTIVE'


def test_same_coin_intents_merge_single_net_order(store):
    # 同币两格同触发(币级信号形态)→ 一次 close_set → 恰 1 张净额市价单
    ex, gx, mgr, rec = _setup(store, [AAA])
    ga = gx.open('fake', AAA, dict(GP), tag='tA')
    gb = gx.open('fake', AAA, dict(GP), tag='tB')
    ex._open.clear()                           # 隔离:清交易所侧线单,防跌价触发梯子成交
    for g in (ga, gb):
        _force_long(ex, gx, g, AAA, 5.0)
    ex.set_price(AAA, 90.0)
    before = len(ex._trades)
    run_monitor_cycle(rec, mgr, log=lambda *a: None, parallel=2)
    new = [t for t in ex._trades[before:] if ':close:' in str(t.client_oid)]
    assert len(new) == 1 and abs(new[0].size - 10.0) < 1e-9   # 净额一张
    assert gx.grids.get(ga).status == 'CLOSED'
    assert gx.grids.get(gb).status == 'CLOSED'
    assert len(gx.records.list_by_grid(ga)) == 1


def test_cross_coin_parallel_no_deadlock(store):
    ex, gx, mgr, rec = _setup(store, [AAA, BBB])
    ga = gx.open('fake', AAA, dict(GP))
    gb = gx.open('fake', BBB, dict(GP))
    _force_long(ex, gx, ga, AAA, 5.0)
    _force_long(ex, gx, gb, BBB, 5.0)
    ex.set_price(AAA, 90.0)
    ex.set_price(BBB, 90.0)
    run_monitor_cycle(rec, mgr, log=lambda *a: None, parallel=4)
    assert gx.grids.get(ga).status == 'CLOSED'
    assert gx.grids.get(gb).status == 'CLOSED'


def test_healthy_grids_unaffected(store):
    ex, gx, mgr, rec = _setup(store, [AAA])
    ga = gx.open('fake', AAA, dict(GP))
    out = run_monitor_cycle(rec, mgr, log=lambda *a: None, parallel=2)
    assert gx.grids.get(ga).status == 'ACTIVE'
    assert not out['degraded']
