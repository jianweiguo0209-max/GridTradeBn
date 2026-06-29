"""Reconciler：重启对账自愈。restore 重建执行器内存态；reconcile_open_orders 按 exchange order id 对账。"""
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
        acc = ex.accounting.get(grid_id)
        if acc is not None:
            live.funding_paid = acc.funding_paid      # recover cumulative funding (durable)
        ex.live[grid_id] = live
        ex._trade_cursor[grid_id] = ex.fills.max_ts(grid_id)
        # 无已推进的游标（acc 缺失或 0=开仓后尚未 sync）时回退到开仓时刻，而非 0，
        # 否则首次 sync 会把开仓前的历史 funding 计入本网格。
        ex._funding_cursor[grid_id] = (acc.funding_cursor if acc is not None and acc.funding_cursor
                                       else g.created_at)

    def reconcile_open_orders(self, grid_id, symbol):
        ex = self.ex
        # 按 exchange order id 对账（跨所通用；HL open order 只带 oid、不带我方 cloid）。
        expected = {o.exchange_order_id: o for o in ex.orders.list_open_by_grid(grid_id)
                    if o.exchange_order_id}
        on_exchange = {o.id: o for o in ex.adapter.fetch_open_orders(symbol)}

        canceled = 0
        for oid, o in on_exchange.items():
            if oid not in expected:
                ex.adapter.cancel_order(symbol, o.id)
                canceled += 1

        replaced = 0
        for oid, go in expected.items():
            if oid not in on_exchange:
                # 先撤旧 oid 再补：HL 抖动时 fetch_open_orders 可能漏返回一张【仍在挂】的单，
                # 直接重挂会产生重复单（旧单后来成交、其 oid 已被新单覆盖 → 漏摄入、净仓漂）。
                # 撤掉（已成交/已撤则 no-op 或报错，吞掉）再补，从根上杜绝重复。
                try:
                    ex.adapter.cancel_order(symbol, oid)
                except Exception:
                    pass
                order = ex.adapter.create_limit_order(symbol, go.side, go.price, go.size,
                                                      post_only=False, client_oid=go.client_oid)
                ex.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                                           line_index=go.line_index, side=go.side, price=go.price,
                                           size=go.size, status='open',
                                           exchange_order_id=getattr(order, 'id', None)))
                replaced += 1
        return {'canceled': canceled, 'replaced': replaced}

    def check_position_drift(self, grid_id, symbol, *, tol_lots=1.5):
        """净仓对账（防御纵深）：比较模型净仓（grid_accounting.net_position）与交易所真实持仓。

        **只读告警**，不自动改仓（自动纠仓风险高，留人工/后续处置）。容差 = tol_lots × 每格量
        （正常 sync 时序内的瞬时差应 < 1 格；持续超过 ~1.5 格即真实背离，如漏摄入成交）。
        无每格量（未 restore）时容差 0。返回 None 表示无法判定（无记账行）。
        """
        ex = self.ex
        acc = ex.accounting.get(grid_id)
        if acc is None:
            return None
        geom = ex._geom.get(grid_id)
        order_num = float(geom['order_num']) if geom else 0.0
        model = float(acc.net_position)
        real = float(ex.adapter.fetch_positions(symbol).net_size)
        drift = model - real
        tol = tol_lots * order_num
        return {'grid_id': grid_id, 'model': model, 'exchange': real,
                'drift': drift, 'tol': tol, 'ok': abs(drift) <= tol}
