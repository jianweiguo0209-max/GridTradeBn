from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
from gridtrade.state.models import ACTIVE


SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    store = StateStore.in_memory(); store.create_all()
    from gridtrade.execution.grid_executor import GridExecutor
    ex_ = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    return ex, store, ex_


def test_open_places_grid_and_neutral_inventory():
    ex, store, gx = _setup(price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP, offset=0, tag='t0')
    # 网格记录 ACTIVE
    from gridtrade.state.grids import GridRepository
    g = GridRepository(store).get(gid)
    assert g.status == ACTIVE and g.entry_price == 100.0
    # 中性底仓：入场价上方 4 条线 × order_num
    on = g.order_num
    pos = ex.fetch_positions(SYM)
    assert abs(pos.net_size - on * 4) < 1e-6
    # 9 条线，entry 不在线上 → 9 个挂单
    opens = ex.fetch_open_orders(SYM)
    assert len(opens) == 9
    sells = [o for o in opens if o.side == 'sell']
    buys = [o for o in opens if o.side == 'buy']
    assert len(sells) == 4 and len(buys) == 5


def test_open_persists_orders_with_client_oid():
    ex, store, gx = _setup()
    gid = gx.open(ex_exchange_name(), SYM, GP)
    from gridtrade.state.orders import OrderRepository
    rows = OrderRepository(store).list_by_grid(gid)
    assert len(rows) == 9
    assert all(r.client_oid.startswith(f'{gid}:') for r in rows)
    assert all(r.status == 'open' for r in rows)


def test_open_undercapitalized_raises():
    import pytest
    ex, store, _ = _setup()
    from gridtrade.execution.grid_executor import GridExecutor
    # min_amount 极大 → 每格量被向下取整到 0 → grid_order_info 返回 None → 建网失败
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, min_amount=1e9)
    with pytest.raises(RuntimeError):
        gx.open(ex_exchange_name(), SYM, GP)


def ex_exchange_name():
    return 'fake'
