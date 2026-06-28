"""Reconciler：重启对账自愈。restore 重建执行器内存态；reconcile_open_orders 按 client_oid 对账。"""
import itertools

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.live_equity import LiveEquity
from gridtrade.state.models import GridOrder


class Reconciler:
    def __init__(self, executor):
        self.ex = executor

    def restore(self, grid_id):
        ex = self.ex
        g = ex.grids.get(grid_id)
        if g is None:
            raise ValueError('grid %s not found' % grid_id)
        gi = grid_order_info(ex.cap, ex.leverage, g.low_price, g.high_price,
                             int(g.grid_count), g.stop_low_price, g.stop_high_price,
                             min_amount=ex.min_amount, max_rate=ex.max_rate)
        price_array = [float(p) for p in gi['价格序列']]
        order_num = float(gi['每笔数量'])
        ex._geom[grid_id] = {'price_array': price_array, 'order_num': order_num}
        ex._seq[grid_id] = itertools.count(10_000_000)  # 高位起，避免与历史 seq 相撞

        live = LiveEquity(ex.cap, ex.fee, ex.c_rate_taker, entry_price=g.entry_price)
        above = [p for p in price_array if p > g.entry_price]
        for _ in range(len(above)):
            live.record_fill(g.entry_price, 'buy', order_num, 0)
        for f in ex.fills.list_by_grid(grid_id):   # 已按 ts 升序
            live.record_fill(f.price, f.side, f.size, f.ts)
        ex.live[grid_id] = live
        ex._trade_cursor[grid_id] = ex.fills.max_ts(grid_id)
        ex._funding_cursor[grid_id] = 0

    def reconcile_open_orders(self, grid_id, symbol):
        ex = self.ex
        expected = {o.client_oid: o for o in ex.orders.list_open_by_grid(grid_id)}
        on_exchange = {o.client_oid: o for o in ex.adapter.fetch_open_orders(symbol)}

        canceled = 0
        for coid, o in on_exchange.items():
            if coid not in expected:
                ex.adapter.cancel_order(symbol, o.id)
                canceled += 1

        replaced = 0
        for coid, go in expected.items():
            if coid not in on_exchange:
                order = ex.adapter.create_limit_order(symbol, go.side, go.price, go.size,
                                                      post_only=False, client_oid=coid)
                ex.orders.upsert(GridOrder(client_oid=coid, grid_id=grid_id,
                                           line_index=go.line_index, side=go.side, price=go.price,
                                           size=go.size, status='open',
                                           exchange_order_id=getattr(order, 'id', None)))
                replaced += 1
        return {'canceled': canceled, 'replaced': replaced}
