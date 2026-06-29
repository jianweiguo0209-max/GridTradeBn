from gridtrade.dashboard.queries import build_overview
from gridtrade.state.grids import GridRepository
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.orders import OrderRepository
from gridtrade.state.models import Grid, GridOrder, ACTIVE


class _PriceAdapter:
    def __init__(self, prices, raise_for=()):
        self._p = prices
        self._raise_for = set(raise_for)

    def fetch_price(self, symbol):
        if symbol in self._raise_for:
            raise RuntimeError("ticker timeout")
        return self._p[symbol]


def test_overview_computes_unrealized_and_stop_distance(store):
    grids = GridRepository(store)
    accs = AccountingRepository(store)
    orders = OrderRepository(store)

    g = grids.create(Grid(id='g1', exchange='hyperliquid', symbol='BTC/USDT:USDT',
                          status=ACTIVE, direction='neutral',
                          low_price=90.0, high_price=110.0,
                          stop_low_price=80.0, stop_high_price=120.0))
    accs.init('g1')
    acc = accs.get('g1')
    acc.net_position = 2.0
    acc.avg_price = 100.0
    acc.realized_pnl = 5.0
    accs.save(acc)
    orders.upsert(GridOrder(client_oid='o1', grid_id='g1', line_index=0,
                            side='buy', price=95.0, size=1.0, status='open'))

    rows = build_overview(store, _PriceAdapter({'BTC/USDT:USDT': 105.0}))
    assert len(rows) == 1
    r = rows[0]
    assert r.grid_id == 'g1'
    assert r.open_order_count == 1
    assert r.current_price == 105.0
    assert r.unrealized_pnl == 10.0          # 2 * (105 - 100)
    assert r.realized_pnl == 5.0
    assert r.price_error is None
    # 现价 105：距上止损 120 -> (120-105)/105 ; 距下止损 80 -> (105-80)/105
    assert round(r.stop_high_dist_pct, 4) == round((120.0 - 105.0) / 105.0, 4)
    assert round(r.stop_low_dist_pct, 4) == round((105.0 - 80.0) / 105.0, 4)


def test_overview_degrades_when_price_unavailable(store):
    grids = GridRepository(store)
    grids.create(Grid(id='g1', exchange='hyperliquid', symbol='ETH/USDT:USDT',
                      status=ACTIVE, direction='neutral'))
    AccountingRepository(store).init('g1')
    rows = build_overview(store, _PriceAdapter({}, raise_for={'ETH/USDT:USDT'}))
    r = rows[0]
    assert r.current_price is None
    assert r.unrealized_pnl is None
    assert r.price_error is not None
