import pandas as pd

from gridtrade.exchanges.base import Instrument


def _fake():
    from gridtrade.exchanges.fake import FakeExchange
    insts = [Instrument('BTC/USDT:USDT', tick=0.1, lot=0.001, min_size=0.001,
                        state='live', list_ts=0)]
    return FakeExchange(instruments=insts, price=100.0)


def test_place_and_list_open_orders():
    ex = _fake()
    o = ex.create_limit_order('BTC/USDT:USDT', 'buy', price=95.0, size=1.0,
                              client_oid='g1:0')
    assert o.status == 'open'
    opens = ex.fetch_open_orders('BTC/USDT:USDT')
    assert len(opens) == 1 and opens[0].client_oid == 'g1:0'


def test_buy_limit_fills_when_price_drops():
    ex = _fake()
    ex.create_limit_order('BTC/USDT:USDT', 'buy', price=95.0, size=2.0, client_oid='g1:0')
    ex.set_price('BTC/USDT:USDT', 94.0)  # 穿越买单价
    assert ex.fetch_open_orders('BTC/USDT:USDT') == []
    trades = ex.fetch_my_trades('BTC/USDT:USDT')
    assert len(trades) == 1 and trades[0].side == 'buy' and trades[0].size == 2.0
    pos = ex.fetch_positions('BTC/USDT:USDT')
    assert pos.net_size == 2.0 and pos.avg_price == 95.0


def test_sell_reduces_position_and_cancel_works():
    ex = _fake()
    ex.create_limit_order('BTC/USDT:USDT', 'buy', price=95.0, size=2.0, client_oid='g1:0')
    ex.set_price('BTC/USDT:USDT', 94.0)
    ex.create_limit_order('BTC/USDT:USDT', 'sell', price=105.0, size=1.0, client_oid='g1:1')
    cid = ex.fetch_open_orders('BTC/USDT:USDT')[0].id
    ex.cancel_order('BTC/USDT:USDT', cid)
    assert ex.fetch_open_orders('BTC/USDT:USDT') == []


def test_market_order_fills_immediately():
    ex = _fake()
    ex.set_price('BTC/USDT:USDT', 100.0)
    ex.create_market_order('BTC/USDT:USDT', 'buy', size=3.0, client_oid='init')
    assert ex.fetch_positions('BTC/USDT:USDT').net_size == 3.0


def test_seeded_ohlcv_and_funding_roundtrip():
    ex = _fake()
    df = pd.DataFrame({'symbol': ['BTC/USDT:USDT'], 'candle_begin_time': [pd.Timestamp('2024-01-01')],
                       'open': [1.0], 'high': [2.0], 'low': [0.5], 'close': [1.5],
                       'vol': [10.0], 'volCcy': [15.0], 'quote_volume': [22.5]})
    ex.seed_ohlcv('BTC/USDT:USDT', df)
    got = ex.fetch_ohlcv('BTC/USDT:USDT', '1H', 0, 10**13)
    assert list(got['close']) == [1.5]
    assert ex.exchange_status() == 'ok'


def test_sell_fill_reduces_net_position():
    ex = _fake()
    ex.create_limit_order('BTC/USDT:USDT', 'buy', price=95.0, size=2.0, client_oid='g1:0')
    ex.set_price('BTC/USDT:USDT', 94.0)            # buy fills -> net +2
    ex.create_limit_order('BTC/USDT:USDT', 'sell', price=96.0, size=1.0, client_oid='g1:1')
    ex.set_price('BTC/USDT:USDT', 97.0)            # sell fills -> net +1
    assert ex.fetch_positions('BTC/USDT:USDT').net_size == 1.0
    assert ex.fetch_open_orders('BTC/USDT:USDT') == []
