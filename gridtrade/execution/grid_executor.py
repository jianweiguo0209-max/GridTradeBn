"""GridExecutor：自管理挂单网格生命周期（开网/同步补单/平网）。
驱动 ExchangeAdapter + 状态层仓储 + LiveEquity。交易所为订单/持仓真相源；
client_oid='{grid_id}:{line}:{seq}' 确定性映射网格线，供对账。
"""
import itertools

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.live_equity import LiveEquity
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import (ACTIVE, Grid, GridOrder, OPENING, now_ms)
from gridtrade.state.orders import OrderRepository


class GridExecutor:
    def __init__(self, adapter, store, *, cap, leverage, fee=0.0002,
                 c_rate_taker=0.0005, max_rate=0.68, min_amount=0.0):
        self.adapter = adapter
        self.grids = GridRepository(store)
        self.orders = OrderRepository(store)
        self.accounting = AccountingRepository(store)
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
        cursor = self._trade_cursor.get(grid_id, 0)
        trades = self.adapter.fetch_my_trades(symbol, since_ms=cursor)
        prefix = '%s:' % grid_id
        new = [t for t in trades
               if t.client_oid.startswith(prefix) and ':init:' not in t.client_oid]
        new.sort(key=lambda t: t.ts)

        for t in new:
            line_index = int(t.client_oid.split(':')[1])
            self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts)
            # 标记成交订单 closed
            self.orders.upsert(GridOrder(client_oid=t.client_oid, grid_id=grid_id,
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

        if new:
            self._trade_cursor[grid_id] = new[-1].ts + 1

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
            self.accounting.save(acc)
            self.accounting.bump_peak(grid_id, snap['pnl_ratio'])
        return {'new_fills': len(new), 'snapshot': snap}
