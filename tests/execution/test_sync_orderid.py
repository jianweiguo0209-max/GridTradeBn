from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument, Trade
from gridtrade.execution.grid_executor import GridExecutor

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    return ex, GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_sync_maps_fill_by_order_id_even_with_opaque_client_oid(store):
    ex, gx = _setup(store, 100.0)
    gid = gx.open('fake', SYM, GP)
    # 取一个已挂的卖单（line 上方），手动注入一笔「client_oid 不可解析、但 order_id 正确」的成交
    open_orders = ex.fetch_open_orders(SYM)
    target = [o for o in open_orders if o.side == 'sell'][0]
    ex._open.pop(target.id, None)                   # 模拟成交：从挂单移除
    ex._trades.append(Trade(id='9001', client_oid='0xdeadbeef-not-grid',
                            symbol=SYM, side='sell', price=target.price,
                            size=target.size, fee=0.0, ts=10_000_000,
                            order_id=target.id))     # 只有 order_id 对得上
    res = gx.sync(gid, SYM)
    assert res['new_fills'] == 1                     # 按 order id 摄入了该成交
