from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    from gridtrade.execution.grid_executor import GridExecutor
    return ex, store, GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_fill_recorded_in_grid_fills(store):
    ex, store, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    fills = gx.fills.list_by_grid(gid)
    assert len(fills) == 1 and fills[0].side == 'sell'


def test_resync_same_trade_not_double_counted(store):
    # 即使游标被人为重置（模拟同毫秒/重复返回），trade_id 去重保证不重复摄入/补单
    ex, store, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6)
    r1 = gx.sync(gid, SYM)
    assert r1['new_fills'] == 1
    open_after_first = len(ex.fetch_open_orders(SYM))
    net_after_first = ex.fetch_positions(SYM).net_size
    # 强制重新拉取同一批成交：把内存游标清零（若存在）
    if hasattr(gx, '_trade_cursor'):
        gx._trade_cursor[gid] = 0
    r2 = gx.sync(gid, SYM)
    assert r2['new_fills'] == 0                       # 去重：无新成交
    assert len(ex.fetch_open_orders(SYM)) == open_after_first   # 未重复补单
    assert abs(ex.fetch_positions(SYM).net_size - net_after_first) < 1e-9
    assert len(gx.fills.list_by_grid(gid)) == 1       # 仍只一条 fill


def test_snapshot_consistent_after_resync(store):
    ex, store, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    snap1 = gx.live[gid].snapshot(ex.fetch_price(SYM))
    if hasattr(gx, '_trade_cursor'):
        gx._trade_cursor[gid] = 0
    gx.sync(gid, SYM)
    snap2 = gx.live[gid].snapshot(ex.fetch_price(SYM))
    assert abs(snap1['net_position'] - snap2['net_position']) < 1e-9
    assert abs(snap1['realized_pnl'] - snap2['realized_pnl']) < 1e-9
