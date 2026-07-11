"""reconcile_fuses 三态对账测试 + reconcile_open_orders 不误撤保险丝。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import CLOSED

SYM = 'BTC/USDC:USDC'
PARAMS = dict(low_price=90.0, high_price=110.0, grid_count=10,
              stop_low_price=80.0, stop_high_price=120.0)


def _open(store):
    fake = FakeExchange()
    fake.set_price(SYM, 100.0)
    ex = GridExecutor(fake, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=True)
    gid = ex.open('hl', SYM, dict(PARAMS))
    return ex, fake, gid


def test_fuses_in_book_no_action(store):
    ex, fake, gid = _open(store)
    rec = Reconciler(ex)
    out = rec.reconcile_fuses(gid, SYM)
    assert out == {'replaced': 0, 'fired': False, 'futile': False}


def test_fired_fuse_tears_down_grid(store):
    ex, fake, gid = _open(store)
    rec = Reconciler(ex)
    fake.set_price(SYM, 79.0)               # 穿破 stop_low -> sell 保险丝触发、平多
    out = rec.reconcile_fuses(gid, SYM)
    assert out['fired'] is True
    assert ex.grids.get(gid).status == CLOSED
    assert not fake.fetch_open_orders(SYM)  # 撑网全拆，网格限价单全撤


def test_dropped_fuse_replaced_not_closed(store):
    ex, fake, gid = _open(store)
    g = ex.grids.get(gid)
    fake._stops[SYM] = [s for s in fake._stops[SYM]
                        if s.id != g.fuse_low_oid]   # 模拟 low 保险丝被交易所丢、无成交
    rec = Reconciler(ex)
    out = rec.reconcile_fuses(gid, SYM)
    assert out['fired'] is False
    assert out['replaced'] == 1
    assert ex.grids.get(gid).status != CLOSED
    new_low = ex.grids.get(gid).fuse_low_oid
    assert new_low != g.fuse_low_oid                 # 回写了新 oid
    assert any(s.id == new_low for s in fake._stops[SYM])


def test_disabled_short_circuits(store):
    fake = FakeExchange(); fake.set_price(SYM, 100.0)
    ex = GridExecutor(fake, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=False)
    gid = ex.open('hl', SYM, dict(PARAMS))
    out = Reconciler(ex).reconcile_fuses(gid, SYM)
    assert out == {'replaced': 0, 'fired': False, 'futile': False}


# ── CRITICAL ADDITION: reconcile_open_orders must NOT cancel fuse stop orders ──

def test_reconcile_open_orders_does_not_cancel_fuses(store):
    """reconcile_open_orders 应跳过保险丝 stop orders，不误撤。

    HL 默认 fetch_open_orders 走 frontendOpenOrders，含 trigger/stop orders；
    FakeExchange.fetch_open_orders 忠实模拟该行为（返回 _stops）。
    保险丝不在 grid_orders（expected）中，如无排除逻辑将被当 unexpected 撤掉。
    """
    ex, fake, gid = _open(store)
    rec = Reconciler(ex)
    g = ex.grids.get(gid)
    low_oid = g.fuse_low_oid
    high_oid = g.fuse_high_oid

    # reconcile_open_orders 后保险丝应仍在交易所
    rec.reconcile_open_orders(gid, SYM)

    stops_on_exchange = {s.id for s in fake._stops.get(SYM, [])}
    assert low_oid in stops_on_exchange,  'fuse_low_oid was wrongly cancelled'
    assert high_oid in stops_on_exchange, 'fuse_high_oid was wrongly cancelled'
