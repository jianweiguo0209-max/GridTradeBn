from gridtrade.exchanges.fake import FakeExchange


def _long_5(ex, sym):
    """建立 +5 的多头持仓，便于测 reduce-only。"""
    ex.set_price(sym, 100.0)
    ex.create_market_order(sym, 'buy', 5.0)


def test_stop_not_filled_until_crossed():
    ex = FakeExchange()
    _long_5(ex, 'X')
    o = ex.create_stop_order('X', 'sell', 5.0, 90.0)   # 跌破 90 才触发
    assert o.status == 'open'
    ex.set_price('X', 95.0)                              # 未穿
    assert ex.fetch_open_orders('X')  # 网格无关单不在这里，但 stop 不应成交
    assert not any(t.order_id == o.id for t in ex.fetch_my_trades('X', since_ms=0))


def test_stop_fills_when_crossed_and_reduces_position():
    ex = FakeExchange()
    _long_5(ex, 'X')
    o = ex.create_stop_order('X', 'sell', 5.0, 90.0)
    ex.set_price('X', 89.0)                              # 穿破触发价
    fills = [t for t in ex.fetch_my_trades('X', since_ms=0) if t.order_id == o.id]
    assert len(fills) == 1
    assert ex.fetch_positions('X').net_size == 0.0       # 多头被平


def test_reduce_only_caps_to_position():
    ex = FakeExchange()
    _long_5(ex, 'X')                                     # 仅 +5
    o = ex.create_stop_order('X', 'sell', 999.0, 90.0)   # size 远超持仓
    ex.set_price('X', 89.0)
    fill = [t for t in ex.fetch_my_trades('X', since_ms=0) if t.order_id == o.id][0]
    assert fill.size == 5.0                               # 封顶到持仓，不反手
    assert ex.fetch_positions('X').net_size == 0.0


def test_reduce_only_noop_without_opposite_position():
    ex = FakeExchange()
    ex.set_price('X', 100.0)                              # 无持仓
    o = ex.create_stop_order('X', 'sell', 5.0, 90.0)
    ex.set_price('X', 89.0)
    assert not any(t.order_id == o.id for t in ex.fetch_my_trades('X', since_ms=0))
    assert o in ex._stops.get('X', [])                   # 空操作，留在簿上


def test_cancel_all_clears_stops():
    ex = FakeExchange()
    _long_5(ex, 'X')
    ex.create_stop_order('X', 'sell', 5.0, 90.0)
    ex.cancel_all('X')
    assert not ex._stops.get('X')
