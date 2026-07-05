# tests/execution/test_sync_snapshot.py
"""sync 快照供给 vs 逐格取数 双路径终态等价（成交摄入/标closed/补单/记账）。"""
import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.snapshot import build_account_snapshot
from gridtrade.state.store import StateStore

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    gid = gx.open('fake', BTC, dict(GP), tag='t0')
    return ex, gx, gid


def _end_state(gx, gid):
    acc = gx.accounting.get(gid)
    return (sorted(f.trade_id for f in gx.fills.list_by_grid(gid)),
            sorted((o.line_index, o.side, o.status) for o in gx.orders.list_by_grid(gid)),
            round(acc.net_position, 9), round(acc.realized_pnl, 9), round(acc.fee_paid, 9))


def test_sync_with_snapshot_equals_plain_sync(store):
    ex1, gx1, g1 = _setup(store)
    st2 = StateStore.in_memory(); st2.create_all()
    try:
        ex2, gx2, g2 = _setup(st2)
        ex1.set_price(BTC, 100.6)          # 同样的卖单成交
        ex2.set_price(BTC, 100.6)
        r1 = gx1.sync(g1, BTC)             # 旧路径
        snap = build_account_snapshot(ex2, [BTC])
        r2 = gx2.sync(g2, BTC, snapshot=snap)   # 快照路径
        assert r1['new_fills'] == r2['new_fills'] == 1
        s1, s2 = _end_state(gx1, g1), _end_state(gx2, g2)
        assert s1[1:] == s2[1:]            # 订单/仓位/盈亏/费用逐项等价
        assert len(s1[0]) == len(s2[0]) == 1
    finally:
        st2.dispose_and_cleanup()


def test_sync_snapshot_missing_price_raises(store):
    ex, gx, gid = _setup(store)
    snap = build_account_snapshot(ex, [])   # 空 symbols → 无 BTC 价格
    with pytest.raises(RuntimeError):
        gx.sync(gid, BTC, snapshot=snap)


def test_sync_snapshot_respects_grid_cursor(store):
    # 快照含全账户成交，本格仍按自己游标过滤 + add_if_new 幂等：不重复摄入
    ex, gx, gid = _setup(store)
    ex.set_price(BTC, 100.6)
    gx.sync(gid, BTC)                       # 先旧路径摄入
    snap = build_account_snapshot(ex, [BTC])
    r = gx.sync(gid, BTC, snapshot=snap)    # 再快照路径跑一轮
    assert r['new_fills'] == 0              # 不重复摄入
