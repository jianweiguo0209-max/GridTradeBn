"""基于 ccxt 统一接口的通用适配器。client 注入以便 mock。
各所差异（凭证/资金费周期/沙盒/符号映射）由子类覆写。"""
import time
from typing import List, Optional

import pandas as pd

from gridtrade.exchanges.base import (Balance, CANDLE_COLS, ExchangeAdapter,
                                      FUNDING_COLS, FundingPayment, Instrument, Order, Position, Trade)


class CcxtAdapter(ExchangeAdapter):
    name = 'ccxt'
    quote_currency = 'USDT'   # 计价/保证金币种；HL 覆写为 USDC

    def __init__(self, client, name: Optional[str] = None):
        self.client = client
        if name:
            self.name = name

    # ---- 符号映射：默认规范符号即 ccxt 统一符号，原样透传 ----
    def to_native(self, symbol: str) -> str:
        return symbol

    def to_canonical(self, native: str) -> str:
        return native

    # ---- 行情 ----
    def list_instruments(self) -> List[Instrument]:
        self.client.load_markets()
        out = []
        for sym, m in self.client.markets.items():
            info = m.get('info', {}) or {}
            out.append(Instrument(
                symbol=self.to_canonical(sym),
                tick=float(m.get('precision', {}).get('price') or 0.0),
                lot=float(m.get('precision', {}).get('amount') or 0.0),
                min_size=float(m.get('limits', {}).get('amount', {}).get('min') or 0.0),
                state='live' if m.get('active', True) else 'expired',
                list_ts=int(info.get('listTime') or 0),
            ))
        return out

    def _now_ms(self):
        # 真实交易所提供 milliseconds()；测试桩可缺省，回退本机时钟。
        ms = getattr(self.client, 'milliseconds', None)
        return int(ms()) if callable(ms) else int(time.time() * 1000)

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms) -> pd.DataFrame:
        native = self.to_native(symbol)
        tf_ms = int(self.client.parse_timeframe(timeframe) * 1000)
        all_rows = []
        cursor = int(start_ms)
        # 不向未来翻页：end_ms 可能是 datasource 给的"今天日终"(未来)，而部分交易所(HL)对
        # since>now 直接 5xx；用真实 now 封顶，分页到现有数据末尾即止。
        bound = min(int(end_ms), self._now_ms())
        guard = 0
        while cursor <= bound and guard < 10000:
            guard += 1
            batch = self.client.fetch_ohlcv(native, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = int(batch[-1][0])
            if last_ts < cursor:          # 无进展
                break
            cursor = last_ts + tf_ms
            if last_ts >= end_ms:         # 已覆盖区间
                break
        if not all_rows:
            return pd.DataFrame(columns=CANDLE_COLS)
        df = pd.DataFrame(all_rows, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df = df.drop_duplicates(subset=['ts'])
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')
        df['symbol'] = symbol
        # ccxt 统一 vol 即真实 base 成交量（OKX 永续 volumeIndex=6 / HL 字段 v），故 volCcy=vol 正确。
        # 报价成交额：OKX 真实 volCcyQuote 经 ccxt 统一接口取不到、HL 无此字段，故用 legacy 文档化
        # 回退 (open+close)/2*vol（见 account_0/utils/stop_loss.py:280）。这样 vwap=quote_volume/volCcy
        # =(open+close)/2 不再恒等于 close，Vwapbias/MarketPl 因子在真实数据上保持有效。
        df['volCcy'] = df['vol']
        df['quote_volume'] = (df['open'] + df['close']) / 2.0 * df['vol']
        df = df[CANDLE_COLS].sort_values('candle_begin_time').reset_index(drop=True)
        return df

    def fetch_funding_history(self, symbol, start_ms, end_ms) -> pd.DataFrame:
        native = self.to_native(symbol)
        all_rows = []
        cursor = int(start_ms)
        bound = min(int(end_ms), self._now_ms())   # 不向未来翻页（见 fetch_ohlcv）
        guard = 0
        while cursor <= bound and guard < 10000:
            guard += 1
            batch = self.client.fetch_funding_rate_history(native, since=cursor, limit=1000)
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = int(batch[-1]['timestamp'])
            if last_ts < cursor:
                break
            cursor = last_ts + 1
            if last_ts >= end_ms:
                break
        if not all_rows:
            return pd.DataFrame(columns=FUNDING_COLS)
        df = pd.DataFrame([{'ts': int(r['timestamp']), 'symbol': symbol,
                            'fundingRate': float(r['fundingRate']),
                            'realizedRate': float(r['fundingRate'])} for r in all_rows])
        df = df.drop_duplicates(subset=['ts'])
        df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
        return df[FUNDING_COLS].sort_values('ts').reset_index(drop=True)

    def fetch_price(self, symbol) -> float:
        return float(self.client.fetch_ticker(self.to_native(symbol))['last'])

    # ---- 账户/交易 ----
    def fetch_balance(self) -> Balance:
        b = self.client.fetch_balance()
        u = b.get(self.quote_currency) or {}   # 键缺失或值为 None 都安全
        return Balance(equity=float(u.get('total') or 0.0), cash=float(u.get('free') or 0.0))

    def fetch_positions(self, symbol) -> Position:
        for p in self.client.fetch_positions([self.to_native(symbol)]):
            if self.to_canonical(p['symbol']) == symbol:
                contracts = float(p.get('contracts') or 0.0)
                net = contracts if p.get('side') == 'long' else -contracts
                return Position(symbol, net, float(p.get('entryPrice') or 0.0))
        return Position(symbol, 0.0, 0.0)

    def _to_order(self, r) -> Order:
        return Order(
            id=str(r['id']),
            client_oid=str(r.get('clientOrderId') or (r.get('info', {}) or {}).get('clOrdId') or r['id']),
            symbol=self.to_canonical(r['symbol']), side=r['side'],
            price=float(r.get('price') or 0.0), size=float(r.get('amount') or 0.0),
            filled=float(r.get('filled') or 0.0), status=r.get('status', 'open'),
            reduce_only=bool((r.get('info', {}) or {}).get('reduceOnly', False)))

    def _params(self, reduce_only, client_oid, post_only=False):
        p = {}
        coid = self.encode_cloid(client_oid) if client_oid else None
        if coid:
            p['clientOrderId'] = coid
        if reduce_only:
            p['reduceOnly'] = True
        if post_only:
            p['postOnly'] = True
        return p

    def create_limit_order(self, symbol, side, price, size, *,
                           post_only=False, reduce_only=False, client_oid=None) -> Order:
        r = self.client.create_order(self.to_native(symbol), 'limit', side, size, price,
                                     self._params(reduce_only, client_oid, post_only))
        return self._to_order(r)

    def create_market_order(self, symbol, side, size, *,
                            reduce_only=False, client_oid=None) -> Order:
        r = self.client.create_order(self.to_native(symbol), 'market', side, size, None,
                                     self._params(reduce_only, client_oid))
        return self._to_order(r)

    def cancel_order(self, symbol, order_id) -> None:
        self.client.cancel_order(order_id, self.to_native(symbol))

    def cancel_all(self, symbol) -> None:
        self.client.cancel_all_orders(self.to_native(symbol))

    def fetch_open_orders(self, symbol) -> List[Order]:
        return [self._to_order(r) for r in self.client.fetch_open_orders(self.to_native(symbol))]

    def fetch_my_trades(self, symbol, since_ms=None) -> List[Trade]:
        out = []
        for r in self.client.fetch_my_trades(self.to_native(symbol), since=since_ms):
            out.append(Trade(
                id=str(r['id']),
                client_oid=str((r.get('info', {}) or {}).get('clOrdId') or r.get('order') or r['id']),
                symbol=self.to_canonical(r['symbol']), side=r['side'],
                price=float(r['price']), size=float(r['amount']),
                fee=float((r.get('fee') or {}).get('cost') or 0.0), ts=int(r['timestamp']),
                order_id=(str(r['order']) if r.get('order') is not None else None)))
        return out

    def set_leverage(self, symbol, leverage) -> None:
        self.client.set_leverage(leverage, self.to_native(symbol))

    def exchange_status(self) -> str:
        return 'ok'

    def fetch_funding_payments(self, symbol, since_ms=None):
        native = self.to_native(symbol)
        rows = self.client.fetch_funding_history(native, since=since_ms)
        out = []
        for r in rows:
            ts = int(r['timestamp'])
            if since_ms is not None and ts < since_ms:
                continue
            # 通用兜底：只保留本币种行（适用于按 symbol 正确打标的交易所）。
            # 注意 HL 例外——它返回账户级全币种且把【查询的 symbol】盖到每行，靠 symbol
            # 区分不出币种，故 HyperliquidAdapter 覆写本方法改按 info.delta.coin 过滤。
            if r.get('symbol') != native:
                continue
            # ccxt 约定 amount 负=支付；统一成"支付为正"
            out.append(FundingPayment(ts=ts, amount=-float(r['amount'])))
        out.sort(key=lambda p: p.ts)
        return out
