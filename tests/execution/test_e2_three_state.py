# tests/execution/test_e2_three_state.py
"""E2 重挂三态升级(spec 2026-07-09,复用 reconcile_fuses 三态模式):
达宽限要重挂前先问 order_status 权威——'filled' 不重挂(杜绝已吃满被误判丢单→
重挂重复建仓,E 系列根源);'open' 盲区不动;canceled/unknown 才撤旧重挂,
且部分成交后只重挂残量(size 校正为 filled+量化残量)。"""
from gridtrade.exchanges.base import Instrument, Trade
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    gid = gx.open('fake', BTC, dict(GP))
    rec = Reconciler(gx, replace_grace=2)
    go = [o for o in gx.orders.list_by_grid(gid)
          if o.side == 'sell' and o.status == 'open'][0]
    return ex, gx, gid, rec, go


def _pass_grace(rec, gid, sym):
    out1 = rec.reconcile_open_orders(gid, sym)     # 第 1 轮 missing:宽限
    out2 = rec.reconcile_open_orders(gid, sym)     # 第 2 轮:达宽限
    return out1, out2


def test_filled_missing_order_not_reposted(store):
    # 已吃满但成交尚未摄入(延迟窗口)→ 权威 'filled' → 不重挂(旧行为=重挂重复建仓)
    ex, gx, gid, rec, go = _setup(store)
    del ex._open[go.exchange_order_id]             # 从 book 消失
    ex._trades.append(Trade(id='fx1', client_oid=go.client_oid, symbol=BTC,
                            side=go.side, price=go.price, size=go.size, fee=0.01,
                            ts=1000, order_id=go.exchange_order_id))
    out1, out2 = _pass_grace(rec, gid, BTC)
    assert out1['replaced'] == 0 and out2['replaced'] == 0
    row = gx.orders.get(go.client_oid)
    assert row.exchange_order_id == go.exchange_order_id   # 行未被重挂覆写
    gx.sync(gid, BTC)                                       # 成交由 sync 摄入并闭合行
    assert gx.orders.get(go.client_oid).status == 'closed'


def test_canceled_missing_order_reposted(store):
    # 无成交、真被丢 → canceled → 撤旧重挂(今日行为保留)
    ex, gx, gid, rec, go = _setup(store)
    del ex._open[go.exchange_order_id]
    out1, out2 = _pass_grace(rec, gid, BTC)
    assert out1['replaced'] == 0 and out2['replaced'] == 1
    row = gx.orders.get(go.client_oid)
    assert row.status == 'open' and row.exchange_order_id != go.exchange_order_id


def test_partial_then_dropped_reposts_remnant_only(store):
    # 部分成交(0.4)后订单被丢:fake 权威会答 'filled'(有成交即 filled 的测试替身
    # 语义)→ 不重挂。真实所答 canceled 的路径:清掉成交记录模拟 → 重挂只补残量。
    ex, gx, gid, rec, go = _setup(store)
    part = go.size * 0.4
    ex._trades.append(Trade(id='pp1', client_oid=go.client_oid, symbol=BTC,
                            side=go.side, price=go.price, size=part, fee=0.01,
                            ts=1000, order_id=go.exchange_order_id))
    gx.sync(gid, BTC)                              # 摄入部分成交,行 filled=0.4×
    row = gx.orders.get(go.client_oid)
    assert row.status == 'open' and abs(row.filled - part) < 1e-9
    del ex._open[go.exchange_order_id]             # 残单被交易所丢弃
    ex._trades = [t for t in ex._trades if t.order_id != go.exchange_order_id]
    _pass_grace(rec, gid, BTC)
    row2 = gx.orders.get(go.client_oid)
    assert row2.status == 'open'
    new_order = ex._open[row2.exchange_order_id]
    assert abs(new_order.size - (go.size - part)) < 1e-9   # 只重挂残量
    assert abs(row2.filled - part) < 1e-9                  # 累计保留
    assert abs(row2.size - go.size) < 1e-9                 # size=filled+残量(fake 无量化)
