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


def test_partial_fill_leaves_remnant_and_records_trade():
    # 部分成交测试钩子（spec 2026-07-15 §3.2）：触价只成交 qty，残量留簿 filled=0，
    # 成交 Trade.order_id=原单 id（执行器按 order_id 映射回网格线）
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange
    BTC = 'BTC/USDT:USDT'
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 1e-6, 1e-6, 'live', 0)], price=100.0)
    o = ex.create_limit_order(BTC, 'buy', 99.0, 10.0, client_oid='g:1')   # 不立即成交（现价100>99）
    hit = ex.partial_fill(BTC, 99.0, 3.0)
    assert hit is True
    # 残单留簿：同 id、剩 7、filled=0、仍 open
    rem = [x for x in ex.fetch_open_orders(BTC) if x.id == o.id]
    assert len(rem) == 1 and abs(rem[0].size - 7.0) < 1e-9 and rem[0].filled == 0.0
    # 成交流水：一笔 size=3、order_id=原单 id、方向 buy
    tr = [t for t in ex.fetch_my_trades(BTC) if t.order_id == o.id]
    assert len(tr) == 1 and abs(tr[0].size - 3.0) < 1e-9 and tr[0].side == 'buy'
    # 净仓 = 已成交部分
    assert abs(ex.fetch_positions(BTC).net_size - 3.0) < 1e-9


def test_partial_fill_miss_returns_false():
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange
    BTC = 'BTC/USDT:USDT'
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 1e-6, 1e-6, 'live', 0)], price=100.0)
    ex.create_limit_order(BTC, 'buy', 99.0, 10.0, client_oid='g:1')
    assert ex.partial_fill(BTC, 88.0, 3.0) is False       # 无该价位挂单
    assert ex.partial_fill(BTC, 99.0, 10.0) is False      # qty>=size 不算部分成交


def test_partial_then_full_via_setprice_closes_order():
    # 残单被 set_price 触及 → 剩余量全额成交，同 order_id 第二笔成交（执行器据此判吃满）
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange
    BTC = 'BTC/USDT:USDT'
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 1e-6, 1e-6, 'live', 0)], price=100.0)
    o = ex.create_limit_order(BTC, 'buy', 99.0, 10.0, client_oid='g:1')
    ex.partial_fill(BTC, 99.0, 3.0)
    ex.set_price(BTC, 99.0)                                # 价格落到 99 → 残 7 全额成交
    tr = sorted((t.size for t in ex.fetch_my_trades(BTC) if t.order_id == o.id))
    assert tr == [3.0, 7.0]                                # 两笔累计 = 原单 10
    assert not [x for x in ex.fetch_open_orders(BTC) if x.id == o.id]   # 已离簿
    assert abs(ex.fetch_positions(BTC).net_size - 10.0) < 1e-9


def test_leverage_tiers_seed_and_fetch_default_empty():
    from gridtrade.exchanges.fake import FakeExchange
    ex = FakeExchange()
    assert ex.fetch_leverage_tiers('BTC/USDT:USDT') == []      # 默认空(fail-open)
    ex.seed_leverage_tiers('BTC/USDT:USDT',
                           [{'maxLeverage': 5, 'maxNotional': 5000.0}])
    assert ex.fetch_leverage_tiers('BTC/USDT:USDT') == [{'maxLeverage': 5, 'maxNotional': 5000.0}]


def test_set_leverage_records_calls():
    from gridtrade.exchanges.fake import FakeExchange
    ex = FakeExchange()
    ex.set_leverage('BTC/USDT:USDT', 4)
    ex.set_leverage('ETH/USDT:USDT', 7)
    assert ex._leverage_calls == [('BTC/USDT:USDT', 4), ('ETH/USDT:USDT', 7)]
