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

        self.grids.transition_status(gid, OPENING, expected_version=grid.version)

        # 中性底仓：入场价上方线数 × 每格量，市价买
        above = [p for p in price_array if p > entry]
        if above:
            self.adapter.create_market_order(symbol, 'buy', order_num * len(above),
                                             client_oid='%s:init:0' % gid)

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
