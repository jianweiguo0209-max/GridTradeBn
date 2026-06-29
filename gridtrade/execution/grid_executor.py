"""GridExecutor：自管理挂单网格生命周期（开网/同步补单/平网）。
驱动 ExchangeAdapter + 状态层仓储 + LiveEquity。交易所为订单/持仓真相源；
client_oid='{grid_id}:{line}:{seq}' 确定性映射网格线，供对账。
"""
import itertools

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.live_equity import LiveEquity
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import (ACTIVE, CLOSED, CLOSING, Fill, Grid, GridOrder, OPENING, Record, now_ms)
from gridtrade.state.orders import OrderRepository
from gridtrade.state.records import RecordRepository


class GridExecutor:
    def __init__(self, adapter, store, *, cap, leverage, fee=0.0002,
                 c_rate_taker=0.0005, max_rate=0.68, min_amount=0.0):
        self.adapter = adapter
        self.grids = GridRepository(store)
        self.orders = OrderRepository(store)
        self.accounting = AccountingRepository(store)
        self.records = RecordRepository(store)
        self.fills = FillRepository(store)
        self.cap = float(cap)
        self.leverage = float(leverage)
        self.fee = float(fee)
        self.c_rate_taker = float(c_rate_taker)
        self.max_rate = float(max_rate)
        self.min_amount = float(min_amount)
        self.live = {}        # grid_id -> LiveEquity
        self._geom = {}       # grid_id -> dict(price_array, order_num)
        self._seq = {}        # grid_id -> itertools.count
        self._trade_cursor = {}
        self._funding_cursor = {}

    def _next_oid(self, grid_id, line_index):
        return '%s:%d:%d' % (grid_id, line_index, next(self._seq[grid_id]))

    def is_loaded(self, grid_id) -> bool:
        """内存态是否已就绪（同进程 open 或 reconciler.restore 重建后）。"""
        return grid_id in self._geom

    def open(self, exchange, symbol, grid_params, *, offset=0, tag=''):
        gi = grid_order_info(self.cap, self.leverage, grid_params['low_price'],
                             grid_params['high_price'], int(grid_params['grid_count']),
                             grid_params['stop_low_price'], grid_params['stop_high_price'],
                             min_amount=self.min_amount, max_rate=self.max_rate)
        if gi is None:
            raise RuntimeError('建网失败：保证金不足')
        price_array = [float(p) for p in gi['价格序列']]
        order_num = float(gi['每笔数量'])
        entry = float(self.adapter.fetch_price(symbol))

        grid = self.grids.create(Grid(
            id='', exchange=exchange, symbol=symbol, status='PENDING', offset=offset, tag=tag,
            entry_price=entry, low_price=grid_params['low_price'], high_price=grid_params['high_price'],
            stop_low_price=grid_params['stop_low_price'], stop_high_price=grid_params['stop_high_price'],
            grid_count=int(grid_params['grid_count']), order_num=order_num,
            leverage=self.leverage, cap=self.cap))
        gid = grid.id
        self.accounting.init(gid)
        self._geom[gid] = {'price_array': price_array, 'order_num': order_num}
        self._seq[gid] = itertools.count()
        self.live[gid] = LiveEquity(self.cap, self.fee, self.c_rate_taker, entry_price=entry)
        self._trade_cursor[gid] = 0
        self._funding_cursor[gid] = 0

        self.grids.transition_status(gid, OPENING, expected_version=grid.version)

        # 中性底仓：入场价上方线数 × 每格量，市价买
        above = [p for p in price_array if p > entry]
        if above:
            self.adapter.create_market_order(symbol, 'buy', order_num * len(above),
                                             client_oid='%s:init:0' % gid)
            for _ in range(len(above)):
                self.live[gid].record_fill(entry, 'buy', order_num, 0)

        # 逐线挂限价单
        for i, p in enumerate(price_array):
            if p > entry:
                side = 'sell'
            elif p < entry:
                side = 'buy'
            else:
                continue
            oid = self._next_oid(gid, i)
            order = self.adapter.create_limit_order(symbol, side, p, order_num,
                                                    post_only=False, client_oid=oid)
            self.orders.upsert(GridOrder(client_oid=oid, grid_id=gid, line_index=i,
                                         side=side, price=p, size=order_num, status='open',
                                         exchange_order_id=getattr(order, 'id', None)))

        g2 = self.grids.get(gid)
        self.grids.transition_status(gid, ACTIVE, expected_version=g2.version)
        return gid

    def sync(self, grid_id, symbol):
        geom = self._geom[grid_id]
        price_array = geom['price_array']
        order_num = geom['order_num']
        cursor = self.fills.max_ts(grid_id)
        trades = self.adapter.fetch_my_trades(symbol, since_ms=cursor)
        # 按 exchange order id 把成交映射回网格线（跨所通用；HL fill 只带 oid，不带 cloid）。
        # 中性底仓/平仓的市价单不在 grid_orders → 其成交 order_id 不在 by_oid，自动排除。
        by_oid = {o.exchange_order_id: o
                  for o in self.orders.list_by_grid(grid_id) if o.exchange_order_id}
        candidates = [t for t in trades if t.order_id in by_oid]
        candidates.sort(key=lambda t: t.ts)

        new_count = 0
        new_fills_payload = []
        for t in candidates:
            go = by_oid[t.order_id]
            line_index = go.line_index
            fill = Fill(trade_id=str(t.id), grid_id=grid_id, line_index=line_index,
                        side=t.side, price=float(t.price), size=float(t.size), ts=int(t.ts))
            if not self.fills.add_if_new(fill):
                continue   # 已摄入：去重，跳过（不重复记账/补单）
            new_count += 1
            new_fills_payload.append({'line_index': line_index, 'side': t.side,
                                      'price': float(t.price), 'size': float(t.size),
                                      'fee': float(t.fee), 'ts': int(t.ts)})
            self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts)
            # 标记成交订单 closed
            self.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                                         line_index=line_index, side=t.side, price=t.price,
                                         size=t.size, status='closed'))
            # 补对侧单
            opp_line = line_index - 1 if t.side == 'sell' else line_index + 1
            if 0 <= opp_line < len(price_array):
                opp_side = 'buy' if t.side == 'sell' else 'sell'
                p = price_array[opp_line]
                oid = self._next_oid(grid_id, opp_line)
                order = self.adapter.create_limit_order(symbol, opp_side, p, order_num,
                                                        post_only=False, client_oid=oid)
                self.orders.upsert(GridOrder(client_oid=oid, grid_id=grid_id, line_index=opp_line,
                                             side=opp_side, price=p, size=order_num, status='open',
                                             exchange_order_id=getattr(order, 'id', None)))

        # 资金费流水
        fcur = self._funding_cursor.get(grid_id, 0)
        pays = self.adapter.fetch_funding_payments(symbol, since_ms=fcur)
        for p in pays:
            self.live[grid_id].add_funding(p.amount)
        if pays:
            self._funding_cursor[grid_id] = pays[-1].ts + 1

        snap = self.live[grid_id].snapshot(float(self.adapter.fetch_price(symbol)))
        acc = self.accounting.get(grid_id)
        if acc is not None:
            acc.realized_pnl = snap['realized_pnl']
            acc.fee_paid = snap['fee_paid']
            acc.funding_paid = snap['funding_paid']
            acc.net_position = snap['net_position']
            acc.avg_price = snap['avg_price']
            acc.funding_cursor = self._funding_cursor.get(grid_id, 0)
            self.accounting.save(acc)
            self.accounting.bump_peak(grid_id, snap['pnl_ratio'])
        return {'new_fills': new_count, 'fills': new_fills_payload, 'snapshot': snap}

    def close(self, grid_id, symbol, reason):
        grid = self.grids.get(grid_id)
        self.grids.transition_status(grid_id, CLOSING, expected_version=grid.version)
        self.adapter.cancel_all(symbol)
        for o in self.orders.list_open_by_grid(grid_id):
            self.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=grid_id,
                                         line_index=o.line_index, side=o.side, price=o.price,
                                         size=o.size, status='canceled'))
        # 平仓后校残仓并有界补平：reduce 市价单可能部分成交（HL 滑点/薄盘），
        # 留残仓且本网格转 CLOSED 后无人认领，故在此重拉持仓、补 reduce 直至 <= min_amount。
        pos = self.adapter.fetch_positions(symbol)
        attempt = 0
        while abs(pos.net_size) > self.min_amount and attempt < 3:
            side = 'sell' if pos.net_size > 0 else 'buy'
            self.adapter.create_market_order(symbol, side, abs(pos.net_size),
                                             reduce_only=True,
                                             client_oid='%s:close:%d' % (grid_id, attempt))
            attempt += 1
            pos = self.adapter.fetch_positions(symbol)
        snap = self.live[grid_id].snapshot(float(self.adapter.fetch_price(symbol)))
        self.records.add(Record(id='', grid_id=grid_id, exchange=grid.exchange, symbol=symbol,
                                tag=grid.tag, offset=grid.offset, opened_at=grid.created_at,
                                closed_at=now_ms(), sz=self.cap, total_pnl=snap['pnl_ratio'] * self.cap,
                                pnl_ratio=snap['pnl_ratio'], exit_reason=reason))
        g2 = self.grids.get(grid_id)
        self.grids.transition_status(grid_id, CLOSED, expected_version=g2.version)
        return {'reason': reason, 'pnl_ratio': snap['pnl_ratio']}
