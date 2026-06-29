from gridtrade.dashboard.queries import build_grid_detail
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.models import Grid, GridOrder, Fill, ACTIVE


def test_detail_returns_orders_sorted_and_fills_recent_first(store):
    GridRepository(store).create(Grid(id='g1', exchange='hyperliquid',
                                      symbol='BTC/USDT:USDT', status=ACTIVE))
    orders = OrderRepository(store)
    orders.upsert(GridOrder(client_oid='o2', grid_id='g1', line_index=2,
                            side='sell', price=110.0, size=1.0, status='open'))
    orders.upsert(GridOrder(client_oid='o1', grid_id='g1', line_index=1,
                            side='buy', price=90.0, size=1.0, status='open'))
    fills = FillRepository(store)
    fills.add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=1, side='buy',
                          price=90.0, size=1.0, ts=1000))
    fills.add_if_new(Fill(trade_id='t2', grid_id='g1', line_index=2, side='sell',
                          price=110.0, size=1.0, ts=2000))
    AccountingRepository(store).init('g1')

    dto = build_grid_detail(store, 'g1')
    assert dto is not None
    assert [o.line_index for o in dto.orders] == [1, 2]
    assert [f.trade_id for f in dto.fills] == ['t2', 't1']   # ts 降序
    assert dto.accounting is not None


def test_detail_returns_none_for_missing_grid(store):
    assert build_grid_detail(store, 'nope') is None
