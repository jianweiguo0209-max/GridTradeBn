from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument

SYM = 'BTC/USDT:USDT'


def _ex():
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(SYM, 100.0)
    return ex


def test_fill_trade_carries_order_id():
    ex = _ex()
    o = ex.create_limit_order(SYM, 'buy', 100.0, 1.0, client_oid='g:1:0')  # 立即成交
    trades = ex.fetch_my_trades(SYM)
    assert len(trades) == 1
    assert trades[0].order_id == o.id          # 成交带所属订单号
    assert trades[0].client_oid == 'g:1:0'     # client_oid 仍在


def test_market_order_fill_carries_order_id():
    ex = _ex()
    o = ex.create_market_order(SYM, 'buy', 2.0, client_oid='g:init:0')
    trades = ex.fetch_my_trades(SYM)
    assert trades[-1].order_id == o.id
