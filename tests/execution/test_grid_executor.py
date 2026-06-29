from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.models import ACTIVE


SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    from gridtrade.execution.grid_executor import GridExecutor
    ex_ = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    return ex, store, ex_


def test_open_places_grid_and_neutral_inventory(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP, offset=0, tag='t0')
    # 网格记录 ACTIVE
    from gridtrade.state.grids import GridRepository
    g = GridRepository(store).get(gid)
    assert g.status == ACTIVE and g.entry_price == 100.0
    # 中性底仓：入场价上方 4 条线 × order_num
    on = g.order_num
    pos = ex.fetch_positions(SYM)
    assert abs(pos.net_size - on * 4) < 1e-6
    # 9 条线，entry 不在线上 → 9 个挂单
    opens = ex.fetch_open_orders(SYM)
    assert len(opens) == 9
    sells = [o for o in opens if o.side == 'sell']
    buys = [o for o in opens if o.side == 'buy']
    assert len(sells) == 4 and len(buys) == 5


def test_open_persists_orders_with_client_oid(store):
    ex, store, gx = _setup(store)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    from gridtrade.state.orders import OrderRepository
    rows = OrderRepository(store).list_by_grid(gid)
    assert len(rows) == 9
    assert all(r.client_oid.startswith(f'{gid}:') for r in rows)
    assert all(r.status == 'open' for r in rows)


def test_open_undercapitalized_raises(store):
    import pytest
    ex, store, _ = _setup(store)
    from gridtrade.execution.grid_executor import GridExecutor
    # min_amount 极大 → 每格量被向下取整到 0 → grid_order_info 返回 None → 建网失败
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, min_amount=1e9)
    with pytest.raises(RuntimeError):
        gx.open(ex_exchange_name(), SYM, GP)


def test_sync_records_fill_and_replenishes(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    before_open = len(ex.fetch_open_orders(SYM))
    ex.set_price(SYM, 100.6)   # 触发 line 5 卖单成交（100.4812）
    res = gx.sync(gid, SYM)
    assert res['new_fills'] == 1
    # 补单：卖成交后总挂单数不变（撤一卖、补一买）
    assert len(ex.fetch_open_orders(SYM)) == before_open
    # LiveEquity 记录了该成交，净仓下降一格量
    from gridtrade.state.grids import GridRepository
    on = GridRepository(store).get(gid).order_num
    assert abs(ex.fetch_positions(SYM).net_size - on * 3) < 1e-6
    # accounting 落了快照
    acc = gx.accounting.get(gid)
    assert acc is not None and abs(acc.net_position - on * 3) < 1e-6
    # LiveEquity snapshot net_position must match the real exchange position
    assert abs(gx.live[gid].snapshot(ex.fetch_price(SYM))['net_position'] - ex.fetch_positions(SYM).net_size) < 1e-6


def test_sync_funding_payments_accumulate(store):
    from gridtrade.state.grids import GridRepository
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    opened = GridRepository(store).get(gid).created_at
    ex.seed_funding_payments(SYM, [(opened + 1, 1.0)])   # 开仓后结算：支付 1 USDT
    gx.sync(gid, SYM)
    acc = gx.accounting.get(gid)
    assert abs(acc.funding_paid - 1.0) < 1e-9


def test_open_excludes_pre_open_funding(store):
    # 开仓前结算的 funding 不得计入本网格（游标从开仓时刻起算，而非 0）。
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    ex.seed_funding_payments(SYM, [(1, 1.0)])   # ts=1：远早于开仓
    gx.sync(gid, SYM)
    acc = gx.accounting.get(gid)
    assert acc.funding_paid == 0.0


def test_sync_idempotent_no_new_fills(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    ex.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    res2 = gx.sync(gid, SYM)   # 第二次无新成交
    assert res2['new_fills'] == 0


def test_sync_funding_payments_idempotent_across_calls(store):
    from gridtrade.state.grids import GridRepository
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    opened = GridRepository(store).get(gid).created_at
    ex.seed_funding_payments(SYM, [(opened + 1, 1.0)])   # 开仓后结算：支付 1 USDT
    gx.sync(gid, SYM)
    first = gx.accounting.get(gid).funding_paid
    gx.sync(gid, SYM)                                 # 第二次：无新资金费流水
    second = gx.accounting.get(gid).funding_paid
    assert abs(first - 1.0) < 1e-9
    assert abs(second - 1.0) < 1e-9, f"funding double-counted: {second}"


def test_close_cancels_orders_flattens_and_records(store):
    from gridtrade.state.models import CLOSED
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    out = gx.close(gid, SYM, '固定止损')
    assert out['reason'] == '固定止损'
    # 所有挂单已撤
    assert ex.fetch_open_orders(SYM) == []
    # 净仓已平
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9
    # 网格 CLOSED，槽位释放
    from gridtrade.state.grids import GridRepository
    assert GridRepository(store).get(gid).status == CLOSED
    assert GridRepository(store).get_active_by_symbol('fake', SYM) is None
    # 留下一条 record
    recs = gx.records.list_by_grid(gid)
    assert len(recs) == 1 and recs[0].exit_reason == '固定止损'


def test_close_then_reopen_same_symbol_ok(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    gx.close(gid, SYM, '手动停止')
    gid2 = gx.open(ex_exchange_name(), SYM, GP)   # 槽位已释放，可再开
    assert gid2 != gid


def test_sync_refetches_late_visible_trade_below_cursor(store):
    # E4：一笔成交"晚可见"、其 ts 低于当前游标（被别的已摄入成交推高的 max_ts）→
    # 游标=max_ts 会把它跳过永久漏；游标留重叠应重新拉到并入账。
    from gridtrade.exchanges.base import Trade
    ex, store, gx = _setup(store, 100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    sells = [o for o in ex.fetch_open_orders(SYM) if o.side == 'sell']
    a, b = sells[0], sells[1]
    ex._trades.append(Trade(id='hi', client_oid=a.client_oid, symbol=SYM, side='sell',
                            price=a.price, size=a.size, fee=0.0, ts=100, order_id=a.id))
    gx.sync(gid, SYM)                              # 摄入高 ts → max_ts=100
    n1 = len(gx.fills.list_by_grid(gid))
    assert any(f.trade_id == 'hi' for f in gx.fills.list_by_grid(gid))
    ex._trades.append(Trade(id='lo', client_oid=b.client_oid, symbol=SYM, side='sell',
                            price=b.price, size=b.size, fee=0.0, ts=50, order_id=b.id))
    gx.sync(gid, SYM)                              # ts=50 < max_ts=100：晚到成交
    n2 = len(gx.fills.list_by_grid(gid))
    assert n2 == n1 + 1, "ts<游标的晚到成交应被游标重叠重新拉到并入账"
    assert any(f.trade_id == 'lo' for f in gx.fills.list_by_grid(gid))


def ex_exchange_name():
    return 'fake'
