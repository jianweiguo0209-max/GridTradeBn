"""部分成交残额补单收口（spec 2026-07-15）：守卫从"对侧有 open 单就跳过"精确化为
"对侧有 filled==0 满额单才跳过"——残额单照挂整额回购单，消除净仓永久偏差 1×order_num。
双倍建仓防护（filled==0 满额单占位不重复挂）是红线，一并钉死。"""
from gridtrade.exchanges.base import Instrument, Order
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.models import GridOrder

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 96.0, 'high_price': 104.0, 'grid_count': 8,
      'stop_low_price': 95.0, 'stop_high_price': 105.0}


def _open_grid(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 1e-6, 1e-6, 'live', 0)], price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, gearing=3.4)
    gid = gx.open('fake', BTC, dict(GP), tag='t')
    return ex, gx, gid


def _line_qty(gx, gid, li, side):
    return sum(float(o.size) - float(o.filled or 0) for o in gx.orders.list_by_grid(gid)
               if o.status == 'open' and o.line_index == li and o.side == side)


def test_remnant_line_gets_full_replenish_restoring_model_position(store):
    # 核心（spec §四实测复刻）：L4 买单部分成交(残额)后卖单成交 → 守卫照挂整额回购单，
    # 终态净仓精确还原 +1.00×order_num（= 正常全额路径），账本零漂移。
    ex, gx, gid = _open_grid(store)
    q = gx._geom[gid]['order_num']; pa = gx._geom[gid]['price_array']
    pk, pk1 = pa[4], pa[5]                         # L4 最高买线、L5 最低卖线
    ex.partial_fill(BTC, pk, q * 0.30); gx.sync(gid, BTC)   # L4 只吃 30%，残额留簿
    ex.set_price(BTC, pk1); gx.sync(gid, BTC)      # 升到 L5 → 卖单成交，触发补 L4 回购单
    # 修复前：守卫见 (L4,buy) 已 open 就跳过 → L4 只有残额 0.7q；修复后：加挂整额回购单
    assert _line_qty(gx, gid, 4, 'buy') > q * 1.5  # 残额 0.7q + 回购 1.0q ≈ 1.7q
    ex.set_price(BTC, pk); gx.sync(gid, BTC)        # 回落 L4 → 两张都成交
    pos = ex.fetch_positions(BTC).net_size
    snap = gx.live[gid].snapshot(pk)
    assert abs(pos - q) < 1e-6                      # 净仓 = +1.00×order_num（还原模型）
    assert abs(snap['net_position'] - pos) < 1e-9  # 账本 == 交易所（零漂移）


def test_full_order_still_blocks_double_build(store):
    # 红线（spec §3.3）：对侧线有 filled==0 满额单时，重复 sync 不得产生第二张单
    # （testnet OP/gt00 双倍建仓事故防护——精确化绝不能削弱）。
    ex, gx, gid = _open_grid(store)
    pa = gx._geom[gid]['price_array']
    ex.set_price(BTC, pa[4]); gx.sync(gid, BTC)     # L4 买单全额成交 → 补 L5 卖 & L3 买（满额）
    n_before = len([o for o in gx.orders.list_by_grid(gid) if o.status == 'open'])
    gx.sync(gid, BTC); gx.sync(gid, BTC)            # 重复 sync：满额单占位，不得重复挂
    n_after = len([o for o in gx.orders.list_by_grid(gid) if o.status == 'open'])
    assert n_after == n_before                      # 挂单数不增（无双倍建仓）
    # 每条 (line,side) 至多一张 open 单
    from collections import Counter
    c = Counter((o.line_index, o.side) for o in gx.orders.list_by_grid(gid) if o.status == 'open')
    assert max(c.values()) == 1


def test_replenish_opposite_path_same_guard(store):
    # _replenish_opposite（E2 兜底路径）同款精确化：残额线不挡整额回购单
    ex, gx, gid = _open_grid(store)
    q = gx._geom[gid]['order_num']
    # 把 L4 买单改成残额态（filled>0、open）
    for o in gx.orders.list_by_grid(gid):
        if o.line_index == 4 and o.side == 'buy' and o.status == 'open':
            gx.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=gid, line_index=4,
                                       side='buy', price=o.price, size=o.size, status='open',
                                       exchange_order_id=o.exchange_order_id, filled=q * 0.3))
    # 直接调 _replenish_opposite 补 L4（模拟 L5 卖单吃满的兜底路径）→ 残额不挡，应补
    assert gx._replenish_opposite(gid, BTC, 5, 'sell') is True
    assert _line_qty(gx, gid, 4, 'buy') > q * 1.5   # 残额 + 整额回购单并存


def test_two_orders_same_line_survive_restart_reconcile(store):
    # 不变量①（spec §四实测）：同线两单（残额+整额回购）经 restore+reconcile → 各带独立
    # exchange_order_id、逐单对账，2 单存活、无误撤/漏挂/重复。
    from gridtrade.execution.reconciler import Reconciler
    ex, gx, gid = _open_grid(store)
    q = gx._geom[gid]['order_num']; pa = gx._geom[gid]['price_array']
    # 造同线两单：L4 残额单(filled=0.3q) + L4 整额回购单
    for o in gx.orders.list_by_grid(gid):
        if o.line_index == 4 and o.side == 'buy' and o.status == 'open':
            gx.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=gid, line_index=4,
                                       side='buy', price=o.price, size=o.size, status='open',
                                       exchange_order_id=o.exchange_order_id, filled=q * 0.3))
    oid2 = gx._next_oid(gid, 4)
    o2 = ex.create_limit_order(BTC, 'buy', pa[4], q, post_only=False, client_oid=oid2)
    gx.orders.upsert(GridOrder(client_oid=oid2, grid_id=gid, line_index=4, side='buy',
                               price=pa[4], size=q, status='open', exchange_order_id=o2.id))
    before = sorted((o.line_index, o.side, o.exchange_order_id) for o in gx.orders.list_open_by_grid(gid))
    # 模拟重启：新 executor + restore + reconcile
    gx2 = GridExecutor(ex, store, cap=1000.0, gearing=3.4)
    rec = Reconciler(gx2); rec.restore(gid); rec.reconcile_open_orders(gid, BTC)
    after = sorted((o.line_index, o.side, o.exchange_order_id) for o in gx2.orders.list_open_by_grid(gid))
    n_l4 = sum(1 for o in gx2.orders.list_open_by_grid(gid) if o.line_index == 4 and o.side == 'buy')
    on_exch = sum(1 for o in ex.fetch_open_orders(BTC) if abs(o.price - pa[4]) < 1e-9)
    assert before == after and n_l4 == 2 and on_exch == 2   # 逐位一致、两单存活、零误撤漏挂
