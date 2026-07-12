# tests/execution/test_quantized_size.py — 量化缺口卡死补单根治
"""memory quantized-size-fallback-bug（2026-07-12 mainnet AVAX gt04 实证）：
HL createOrder 响应不带数量 → "存回传 amount"退化为存原始 order_num；交易所按
szDecimals 量化成交（35.10442 → 35.1）→ 吃满判定永假 → 线卡死不补对侧单（呼吸停摆）。
修复：①下单前统一自量化（开格/sync 补单/E2 残量重挂）②E2 权威 'filled' 兜底闭合+补对侧。"""
from gridtrade.exchanges.base import Instrument, Order, Trade
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import GridOrder

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


class _HLLikeExchange(FakeExchange):
    """贴近 HL 语义的替身：①数量精度 1 位小数②create 响应不带数量（size=0——
    正是触发 fallback 的形状）。"""

    def quantize_amount(self, symbol, amount):
        return float(int(float(amount) * 10)) / 10.0        # 向下截断到 1 位小数

    def create_limit_order(self, symbol, side, price, size, *, post_only=False,
                           reduce_only=False, client_oid=None):
        o = super().create_limit_order(symbol, side, price, size, post_only=post_only,
                                       reduce_only=reduce_only, client_oid=client_oid)
        return Order(id=o.id, client_oid=o.client_oid, symbol=o.symbol, side=o.side,
                     price=o.price, size=0.0, filled=o.filled, status=o.status,
                     reduce_only=o.reduce_only)             # 响应抹掉数量(HL 形状)


def _setup(store):
    ex = _HLLikeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                         price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)   # order_num≈3.78,量化→3.7
    gid = gx.open('fake', BTC, dict(GP))
    return ex, gx, gid


def _fill_full(ex, go, tid, ts):
    ex._trades.append(Trade(id=tid, client_oid=go.client_oid, symbol=BTC,
                            side=go.side, price=go.price, size=go.size, fee=0.01,
                            ts=ts, order_id=go.exchange_order_id))


def _vacate(ex, gx, gid, line, side):
    for o in gx.orders.list_by_grid(gid):
        if o.line_index == line and o.side == side and o.status == 'open':
            ex.cancel_order(BTC, o.exchange_order_id)
            gx.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=gid,
                                       line_index=o.line_index, side=o.side,
                                       price=o.price, size=o.size, status='canceled',
                                       exchange_order_id=o.exchange_order_id))


def test_open_stores_quantized_size(store):
    """开格行 size = 自量化值（1 位小数），不再是原始 order_num。"""
    ex, gx, gid = _setup(store)
    rows = [o for o in gx.orders.list_by_grid(gid) if o.status == 'open']
    assert rows
    for o in rows:
        assert abs(o.size * 10 - round(o.size * 10)) < 1e-9, \
            'size 未量化: %r' % o.size                       # 修复前=3.7827…原始值,失败


def test_quantized_fill_closes_and_replenishes(store):
    """量化整量成交 → 吃满判定为真 → 行 closed + 对侧补单出现（呼吸恢复）。
    修复前:size 存原始值,fill(量化值) 永远差一口 → 行卡 open、零补单。"""
    ex, gx, gid = _setup(store)
    sells = [o for o in gx.orders.list_by_grid(gid) if o.side == 'sell' and o.status == 'open']
    go = sells[0]
    _vacate(ex, gx, gid, go.line_index - 1, 'buy')          # 腾出对侧,让补单可见
    n_before = len(gx.orders.list_by_grid(gid))
    _fill_full(ex, go, 'q1', 1000)                           # 成交=行 size(已量化)
    gx.sync(gid, BTC)
    row = gx.orders.get(go.client_oid)
    assert row.status == 'closed'                            # 吃满闭合
    new_rows = [o for o in gx.orders.list_by_grid(gid)
                if o.line_index == go.line_index - 1 and o.side == 'buy' and o.status == 'open']
    assert len(new_rows) == 1                                # 对侧补单已挂
    assert abs(new_rows[0].size * 10 - round(new_rows[0].size * 10)) < 1e-9  # 补单也量化
    assert len(gx.orders.list_by_grid(gid)) == n_before + 1


def test_e2_filled_finalizes_stuck_legacy_row(store):
    """E2 兜底：历史卡死行（size=未量化原始值、filled=量化成交、行 open、单已离场、
    交易所权威 status='filled'）→ 达宽限后闭合 + 腾线 + 补对侧。"""
    ex, gx, gid = _setup(store)
    sells = [o for o in gx.orders.list_by_grid(gid) if o.side == 'sell' and o.status == 'open']
    go = sells[0]
    _vacate(ex, gx, gid, go.line_index - 1, 'buy')
    # 构造 v30 时代的卡死形状：行 size 改回未量化原始值、filled=量化值(差一口)
    raw = go.size + 0.0442
    stuck = GridOrder(client_oid=go.client_oid, grid_id=gid, line_index=go.line_index,
                      side=go.side, price=go.price, size=raw, status='open',
                      exchange_order_id=go.exchange_order_id, filled=0.0)
    gx.orders.upsert(stuck)                                  # filled 由下面 sync 真实摄入
    _fill_full(ex, go, 'q2', 2000)                           # 成交在案(order_status→filled)
    ex._open.pop(go.exchange_order_id, None)                 # 单已离开挂单簿
    gx.sync(gid, BTC)                                        # sync 吃不满(差 0.0442),行仍 open
    assert gx.orders.get(go.client_oid).status == 'open'
    rec = Reconciler(gx, replace_grace=2)
    rec.reconcile_open_orders(gid, BTC)                      # 轮1:宽限
    out = rec.reconcile_open_orders(gid, BTC)                # 轮2:权威 filled → 兜底
    row = gx.orders.get(go.client_oid)
    assert row.status == 'closed'                            # 闭合(修复前:永远 open)
    assert row.exchange_order_id == go.exchange_order_id     # 字段保真
    news = [o for o in gx.orders.list_by_grid(gid)
            if o.line_index == go.line_index - 1 and o.side == 'buy' and o.status == 'open']
    assert len(news) == 1                                    # 呼吸恢复:对侧补单已挂
    assert out['replaced'] >= 1


def test_e2_filled_respects_opposite_guard(store):
    """兜底补对侧同样受「对侧已占用」守卫——不产生同线双单。"""
    ex, gx, gid = _setup(store)
    sells = [o for o in gx.orders.list_by_grid(gid) if o.side == 'sell' and o.status == 'open']
    go = sells[0]                                            # 不腾对侧:初始 buy 仍在
    raw = go.size + 0.0442
    gx.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=gid,
                               line_index=go.line_index, side=go.side, price=go.price,
                               size=raw, status='open',
                               exchange_order_id=go.exchange_order_id, filled=0.0))
    _fill_full(ex, go, 'q3', 3000)
    ex._open.pop(go.exchange_order_id, None)
    gx.sync(gid, BTC)
    rec = Reconciler(gx, replace_grace=2)
    rec.reconcile_open_orders(gid, BTC)
    rec.reconcile_open_orders(gid, BTC)
    assert gx.orders.get(go.client_oid).status == 'closed'
    opp = [o for o in gx.orders.list_by_grid(gid)
           if o.line_index == go.line_index - 1 and o.side == 'buy' and o.status == 'open']
    assert len(opp) == 1                                     # 只有原有那张,无重复
