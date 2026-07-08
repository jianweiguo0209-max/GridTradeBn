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


def test_overview_row_exposes_open_time(store):
    grids = GridRepository(store)
    grids.create(Grid(id='g1', exchange='hyperliquid', symbol='BTC/USDT:USDT',
                      status=ACTIVE, direction='neutral'))
    AccountingRepository(store).init('g1')
    rows = build_overview(store, _PriceAdapter({'BTC/USDT:USDT': 100.0}))
    # 开盘时间（created_at, ms）暴露给列表用于显示
    assert rows[0].created_at == grids.get('g1').created_at
    assert isinstance(rows[0].created_at, int) and rows[0].created_at > 0


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


def test_overview_treats_nonpositive_price_as_unavailable(store):
    grids = GridRepository(store)
    grids.create(Grid(id='g1', exchange='hyperliquid', symbol='BTC/USDT:USDT',
                      status=ACTIVE, direction='neutral',
                      stop_low_price=80.0, stop_high_price=120.0))
    AccountingRepository(store).init('g1')
    rows = build_overview(store, _PriceAdapter({'BTC/USDT:USDT': 0.0}))
    r = rows[0]
    assert r.current_price is None
    assert r.unrealized_pnl is None
    assert r.stop_low_dist_pct is None
    assert r.stop_high_dist_pct is None
    assert r.price_error is not None


class _BatchAdapter(_PriceAdapter):
    """带批量取价的适配器（HL 形）：build_overview 应整页一次批量、不逐格调 fetch_price。"""
    def __init__(self, prices):
        super().__init__(prices)
        self.batch_calls = 0
        self.single_calls = 0

    def fetch_prices_all(self, symbols):
        self.batch_calls += 1
        return {s: self._p[s] for s in symbols if s in self._p}

    def fetch_price(self, symbol):
        self.single_calls += 1
        return super().fetch_price(symbol)


def test_overview_uses_batch_prices_once(store):
    # 逐格 fetch_price 串行在主 dex 币实测 ~12s/个（2026-07-08 首页 73.6s 事故）；
    # 有 fetch_prices_all 时必须整页一次批量，零逐格调用。
    grids = GridRepository(store)
    accs = AccountingRepository(store)
    for i, sym in enumerate(('BTC/USDT:USDT', 'ETH/USDT:USDT')):
        grids.create(Grid(id='b%d' % i, exchange='hyperliquid', symbol=sym,
                          status=ACTIVE, direction='neutral',
                          low_price=90.0, high_price=110.0,
                          stop_low_price=80.0, stop_high_price=120.0))
        accs.init('b%d' % i)
    ad = _BatchAdapter({'BTC/USDT:USDT': 105.0, 'ETH/USDT:USDT': 4000.0})
    rows = build_overview(store, ad)
    assert ad.batch_calls == 1 and ad.single_calls == 0
    assert {r.current_price for r in rows} == {105.0, 4000.0}


def test_overview_batch_missing_falls_back_per_symbol(store):
    # 批量缺币（罕见）→ 该币逐格回退原路径，其余不受影响。
    grids = GridRepository(store)
    accs = AccountingRepository(store)
    grids.create(Grid(id='m1', exchange='hyperliquid', symbol='BTC/USDT:USDT',
                      status=ACTIVE, direction='neutral',
                      low_price=90.0, high_price=110.0,
                      stop_low_price=80.0, stop_high_price=120.0))
    accs.init('m1')
    class _EmptyBatch(_BatchAdapter):
        def fetch_prices_all(self, symbols):
            self.batch_calls += 1
            return {}                   # 批量一无所获 → 逐格回退

    ad = _EmptyBatch({'BTC/USDT:USDT': 105.0})
    rows = build_overview(store, ad)
    assert ad.batch_calls == 1 and ad.single_calls == 1
    assert rows[0].current_price == 105.0


def test_overview_sorted_by_created_at_desc(store):
    """active grids 按建网时间倒序(最新在上)——用户要求 2026-07-08。"""
    import time
    grids = GridRepository(store)
    accs = AccountingRepository(store)
    for i, sym in enumerate(['AAA/USDT:USDT', 'ZZZ/USDT:USDT', 'MMM/USDT:USDT']):
        g = grids.create(Grid(id='g%d' % i, exchange='hyperliquid', symbol=sym,
                              status=ACTIVE, direction='neutral',
                              low_price=90.0, high_price=110.0,
                              stop_low_price=80.0, stop_high_price=120.0))
        accs.init(g.id)
        time.sleep(0.002)                       # created_at(ms) 单调递增
    rows = build_overview(store, _PriceAdapter({s: 100.0 for s in
                          ['AAA/USDT:USDT', 'ZZZ/USDT:USDT', 'MMM/USDT:USDT']}))
    assert [r.symbol for r in rows] == ['MMM/USDT:USDT', 'ZZZ/USDT:USDT', 'AAA/USDT:USDT']
