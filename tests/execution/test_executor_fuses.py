import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler

PARAMS = dict(low_price=90.0, high_price=110.0, grid_count=10,
              stop_low_price=80.0, stop_high_price=120.0)


def _executor(store, **kw):
    ex = FakeExchange()
    ex.set_price('BTC/USDC:USDC', 100.0)
    return GridExecutor(ex, store, cap=1000.0, leverage=5.0, **kw), ex


def test_open_places_two_fuses_when_enabled(store):
    ex, fake = _executor(store, stop_orders_enabled=True)
    gid = ex.open('hl', 'BTC/USDC:USDC', dict(PARAMS))
    stops = fake._stops['BTC/USDC:USDC']
    sides = sorted((s.side, s.price) for s in stops)
    assert sides == [('buy', 120.0), ('sell', 80.0)]      # buy@stop_high, sell@stop_low
    worst = ex.grids.get(gid).grid_count * ex.grids.get(gid).order_num
    assert all(s.size == worst for s in stops)
    g = ex.grids.get(gid)
    assert g.fuse_low_oid is not None and g.fuse_high_oid is not None
    assert ex._fuses[gid]['low'] == g.fuse_low_oid


def test_open_no_fuses_when_disabled(store):
    ex, fake = _executor(store, stop_orders_enabled=False)
    gid = ex.open('hl', 'BTC/USDC:USDC', dict(PARAMS))
    assert not fake._stops.get('BTC/USDC:USDC')
    g = ex.grids.get(gid)
    assert g.fuse_low_oid is None and g.fuse_high_oid is None


def test_close_cancels_surviving_fuses(store):
    ex, fake = _executor(store, stop_orders_enabled=True)
    gid = ex.open('hl', 'BTC/USDC:USDC', dict(PARAMS))
    ex.close(gid, 'BTC/USDC:USDC', '测试')
    assert not fake._stops.get('BTC/USDC:USDC')           # cancel_all 已清


def test_restore_rebuilds_fuse_cache(store):
    ex, fake = _executor(store, stop_orders_enabled=True)
    gid = ex.open('hl', 'BTC/USDC:USDC', dict(PARAMS))
    g = ex.grids.get(gid)
    ex._fuses.clear()                                      # 模拟新进程：内存态丢失
    Reconciler(ex).restore(gid)
    assert ex._fuses[gid]['low'] == g.fuse_low_oid
    assert ex._fuses[gid]['high'] == g.fuse_high_oid
