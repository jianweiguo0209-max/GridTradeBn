"""内存交易所模拟器：实现 ExchangeAdapter，供执行/对账/止损离线 TDD，并与回测填单同源。
撮合规则：buy 当现价<=买单价成交；sell 当现价>=卖单价成交（限价单被价格穿越即成交）。
Trade ts is a monotonic logical counter (not epoch ms); callers should pass a previously-returned trade ts as since_ms to fetch_my_trades.
"""
import itertools
from typing import Dict, List, Optional

import pandas as pd

from gridtrade.exchanges.base import (Balance, ExchangeAdapter, Instrument,
                                      Order, Position, Trade, FundingPayment)


class FakeExchange(ExchangeAdapter):
    name = 'fake'

    def __init__(self, instruments: Optional[List[Instrument]] = None, price: float = 100.0):
        self._instruments = list(instruments or [])
        self._price: Dict[str, float] = {}
        self._open: Dict[str, Order] = {}          # order_id -> Order
        self._trades: List[Trade] = []
        self._pos: Dict[str, Position] = {}
        self._ohlcv: Dict[str, pd.DataFrame] = {}
        self._funding: Dict[str, pd.DataFrame] = {}
        self._funding_payments = {}
        self._ids = itertools.count(1)
        self._ts = itertools.count(1)
        self._fee_rate = 0.0005
        self._default_price = price
        self._stops = {}
        self._quote_volumes = {}

    # ---- 测试钩子 ----
    def set_price(self, symbol: str, price: float) -> None:
        self._price[symbol] = price
        self._match(symbol, price)
        self._check_stops(symbol, price)

    def seed_ohlcv(self, symbol: str, df: pd.DataFrame) -> None:
        self._ohlcv[symbol] = df.copy()

    def seed_funding(self, symbol: str, df: pd.DataFrame) -> None:
        self._funding[symbol] = df.copy()

    def seed_funding_payments(self, symbol, payments):
        self._funding_payments[symbol] = [tuple(p) for p in payments]

    def seed_quote_volumes(self, vols: dict) -> None:
        self._quote_volumes = dict(vols)

    def _price_of(self, symbol: str) -> float:
        return self._price.get(symbol, self._default_price)

    # ---- 撮合 ----
    def _match(self, symbol: str, price: float) -> None:
        for oid in list(self._open.keys()):
            o = self._open[oid]
            if o.symbol != symbol:
                continue
            hit = (o.side == 'buy' and price <= o.price) or (o.side == 'sell' and price >= o.price)
            if hit:
                self._fill(o, o.price)
                del self._open[oid]

    def _fill(self, o: Order, fill_price: float) -> None:
        signed = o.size if o.side == 'buy' else -o.size
        pos = self._pos.get(o.symbol, Position(o.symbol, 0.0, 0.0))
        new_net = pos.net_size + signed
        # 同向加仓更新加权均价；反向或反手时简单处理（净仓符号不翻转的减仓保留均价）
        if pos.net_size == 0 or (pos.net_size > 0) == (signed > 0):
            denom = abs(new_net) if new_net != 0 else 1.0
            avg = (abs(pos.net_size) * pos.avg_price + abs(signed) * fill_price) / denom
        else:
            avg = pos.avg_price if (pos.net_size > 0) == (new_net >= 0) else fill_price
        self._pos[o.symbol] = Position(o.symbol, new_net, avg)
        tid = next(self._ts)
        self._trades.append(Trade(
            id=str(tid), client_oid=o.client_oid, symbol=o.symbol,
            side=o.side, price=fill_price, size=o.size,
            fee=o.size * fill_price * self._fee_rate, ts=tid, order_id=o.id))

    def create_stop_order(self, symbol, side, size, trigger_price, *,
                          reduce_only=True, slippage=0.15, client_oid=None) -> Order:
        oid = str(next(self._ids))
        o = Order(id=oid, client_oid=client_oid or oid, symbol=symbol, side=side,
                  price=trigger_price, size=size, filled=0.0, status='open',
                  reduce_only=reduce_only)
        self._stops.setdefault(symbol, []).append(o)
        return o

    def _check_stops(self, symbol: str, price: float) -> None:
        for o in list(self._stops.get(symbol, [])):
            crossed = (o.side == 'sell' and price <= o.price) or \
                      (o.side == 'buy' and price >= o.price)
            if not crossed:
                continue
            pos = self._pos.get(symbol, Position(symbol, 0.0, 0.0))
            if o.reduce_only:
                # 只在有反向持仓时成交，size 封顶到持仓
                if o.side == 'sell' and pos.net_size > 0:
                    fill_size = min(o.size, pos.net_size)
                elif o.side == 'buy' and pos.net_size < 0:
                    fill_size = min(o.size, -pos.net_size)
                else:
                    continue   # 无可减仓位 -> 空操作，留在簿上
            else:
                fill_size = o.size
            filled = Order(id=o.id, client_oid=o.client_oid, symbol=symbol,
                           side=o.side, price=o.price, size=fill_size, filled=fill_size,
                           status='closed', reduce_only=o.reduce_only)
            self._fill(filled, price)
            self._stops[symbol].remove(o)

    # ---- 行情 ----
    def list_instruments(self) -> List[Instrument]:
        return list(self._instruments)

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        return self._ohlcv.get(symbol, pd.DataFrame()).copy()

    def fetch_funding_history(self, symbol, start_ms, end_ms):
        return self._funding.get(symbol, pd.DataFrame()).copy()

    def fetch_price(self, symbol) -> float:
        return self._price_of(symbol)

    def fetch_24h_quote_volumes(self) -> dict:
        return dict(self._quote_volumes)

    # ---- 账户/交易 ----
    def fetch_balance(self) -> Balance:
        return Balance(equity=1_000_000.0, cash=1_000_000.0)

    def fetch_positions(self, symbol) -> Position:
        return self._pos.get(symbol, Position(symbol, 0.0, 0.0))

    def create_limit_order(self, symbol, side, price, size, *,
                           post_only=False, reduce_only=False, client_oid=None) -> Order:
        if client_oid is not None:                       # 幂等：同 client_oid 已有未成交挂单 -> 返回原单
            for o in self._open.values():
                if o.client_oid == client_oid:
                    return o
        oid = str(next(self._ids))
        o = Order(id=oid, client_oid=client_oid or oid, symbol=symbol, side=side,
                  price=price, size=size, filled=0.0, status='open', reduce_only=reduce_only)
        self._open[oid] = o
        # 下单即按当前价检查是否立即成交
        self._match(symbol, self._price_of(symbol))
        return o if oid in self._open else Order(
            id=oid, client_oid=o.client_oid, symbol=symbol, side=side, price=price,
            size=size, filled=size, status='closed', reduce_only=reduce_only)

    def create_market_order(self, symbol, side, size, *,
                            reduce_only=False, client_oid=None) -> Order:
        oid = str(next(self._ids))
        o = Order(id=oid, client_oid=client_oid or oid, symbol=symbol, side=side,
                  price=self._price_of(symbol), size=size, filled=size,
                  status='closed', reduce_only=reduce_only)
        self._fill(o, self._price_of(symbol))
        return o

    def cancel_order(self, symbol, order_id) -> None:
        self._open.pop(order_id, None)
        # 同时撤止损单（忠实模拟 HL cancel_order 对 trigger/stop 单同样生效）
        if symbol in self._stops:
            self._stops[symbol] = [s for s in self._stops[symbol] if s.id != order_id]

    def cancel_all(self, symbol) -> None:
        for oid in [k for k, v in self._open.items() if v.symbol == symbol]:
            del self._open[oid]
        self._stops.pop(symbol, None)

    def order_status(self, symbol, order_id) -> str:
        # 测试替身语义：有成交记录=filled；仍在 book（限价/触发）=open；否则视为 canceled。
        if any(t.order_id == order_id for t in self._trades):
            return 'filled'
        if order_id in self._open:
            return 'open'
        if any(s.id == order_id for ss in self._stops.values() for s in ss):
            return 'open'
        return 'canceled'

    def fetch_open_orders(self, symbol) -> List[Order]:
        # 忠实镜像 HL 默认的 frontendOpenOrders：同时返回限价单与 trigger/stop 单。
        return ([o for o in self._open.values() if o.symbol == symbol]
                + list(self._stops.get(symbol, [])))

    def fetch_my_trades(self, symbol, since_ms=None) -> List[Trade]:
        return [t for t in self._trades if t.symbol == symbol
                and (since_ms is None or t.ts >= since_ms)]

    def set_leverage(self, symbol, leverage) -> None:
        pass

    def exchange_status(self) -> str:
        return 'ok'

    def fetch_funding_payments(self, symbol, since_ms=None):
        rows = self._funding_payments.get(symbol, [])
        out = [FundingPayment(ts=int(ts), amount=float(amt)) for ts, amt in rows
               if since_ms is None or int(ts) >= since_ms]
        out.sort(key=lambda p: p.ts)
        return out
