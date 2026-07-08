# tests/execution/test_partial_fills.py
"""部分成交生命周期(spec 2026-07-09,mainnet GRAM 实证根治):
同一挂单分多笔部分成交(可跨 sync 轮)→ 全部摄入;订单行累计 filled、吃满才 closed;
行字段保真(exchange_order_id/side/price/size 不被成交覆写);补单只在吃满时触发一次。
旧 bug:首笔部分成交即把行 upsert 成 closed 且抹掉 oid → 跨轮后续部分成交无从匹配被静默丢。"""
from gridtrade.exchanges.base import Instrument, Trade
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.models import GridOrder

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    gid = gx.open('fake', BTC, dict(GP))
    # 取一张在场卖单(不动价格,靠手工追加成交模拟部分成交,残单保持 resting)
    sells = [o for o in gx.orders.list_by_grid(gid) if o.side == 'sell' and o.status == 'open']
    go = sells[0]
    return ex, gx, gid, go


def _partial(ex, go, size, tid, ts):
    ex._trades.append(Trade(id=tid, client_oid=go.client_oid, symbol=BTC,
                            side=go.side, price=go.price, size=size, fee=0.01,
                            ts=ts, order_id=go.exchange_order_id))


def _vacate_opp(ex, gx, gid, go):
    """腾空目标单的对侧线(开格时全线占用,补单会被查重守卫正确跳过——
    为验证「吃满才补单」需要先制造空位)。"""
    opp_line = go.line_index - 1 if go.side == 'sell' else go.line_index + 1
    for o in gx.orders.list_by_grid(gid):
        if o.line_index == opp_line and o.status == 'open':
            ex.cancel_order(BTC, o.exchange_order_id)
            gx.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=gid,
                                       line_index=o.line_index, side=o.side,
                                       price=o.price, size=o.size, status='canceled',
                                       exchange_order_id=o.exchange_order_id))


def test_cross_round_partials_all_ingested(store):
    ex, gx, gid, go = _setup(store)
    total = go.size
    _vacate_opp(ex, gx, gid, go)
    orders_before = len(gx.orders.list_by_grid(gid))

    _partial(ex, go, total * 0.4, 'p1', 1000)
    gx.sync(gid, BTC)                                  # 轮1:摄入部分成交
    row = gx.orders.get(go.client_oid)
    assert row.status == 'open'                        # 未吃满不 closed
    assert abs(row.filled - total * 0.4) < 1e-9
    assert row.exchange_order_id == go.exchange_order_id   # oid 保真(旧 bug 在此抹 None)
    assert abs(row.size - total) < 1e-12               # size 不被 t.size 覆写
    assert len(gx.orders.list_by_grid(gid)) == orders_before   # 未吃满不补单

    _partial(ex, go, total * 0.35, 'p2', 2000)
    gx.sync(gid, BTC)                                  # 轮2:跨轮部分成交仍可匹配
    row = gx.orders.get(go.client_oid)
    assert row.status == 'open'
    assert abs(row.filled - total * 0.75) < 1e-9

    _partial(ex, go, total * 0.25, 'p3', 3000)
    gx.sync(gid, BTC)                                  # 轮3:吃满
    row = gx.orders.get(go.client_oid)
    assert row.status == 'closed'
    assert abs(row.filled - total) < 1e-9
    # 三笔全摄入 → 账本净空 = -total(旧 bug 下第三笔丢,只有 -0.75×total)
    assert abs(gx.live[gid].net_position + total) < 1e-9
    assert len([f for f in gx.fills.list_by_grid(gid)]) == 3
    # 吃满才补单,且只补一次
    assert len(gx.orders.list_by_grid(gid)) == orders_before + 1


def test_same_round_partials_accumulate(store):
    ex, gx, gid, go = _setup(store)
    total = go.size
    _partial(ex, go, total * 0.5, 'q1', 1000)
    _partial(ex, go, total * 0.5, 'q2', 1001)
    gx.sync(gid, BTC)                                  # 同轮两笔累计
    row = gx.orders.get(go.client_oid)
    assert row.status == 'closed'
    assert abs(row.filled - total) < 1e-9
    assert abs(gx.live[gid].net_position + total) < 1e-9


def test_full_fill_single_shot_unchanged(store):
    # 全量成交(常态):一笔吃满 → closed+补单,与旧行为等价
    ex, gx, gid, go = _setup(store)
    _vacate_opp(ex, gx, gid, go)
    orders_before = len(gx.orders.list_by_grid(gid))
    _partial(ex, go, go.size, 'f1', 1000)
    gx.sync(gid, BTC)
    row = gx.orders.get(go.client_oid)
    assert row.status == 'closed'
    assert len(gx.orders.list_by_grid(gid)) == orders_before + 1
