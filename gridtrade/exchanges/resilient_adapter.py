"""ResilientAdapter：把 P4f 健壮性（退避重试 + 熔断）包到内层 ExchangeAdapter 每个调用。

execution/runtime 拿到本适配器即天然健壮（需求 1）。重试安全靠 client_oid 幂等。
"""
import time
from typing import List, Optional

import pandas as pd

from gridtrade.exchanges.base import (Balance, ExchangeAdapter, FundingPayment,
                                      Instrument, Order, Position, Trade)
from gridtrade.exchanges.resilience import RetryPolicy, call_with_retry


class ResilientAdapter(ExchangeAdapter):
    def __init__(self, inner, *, policy=None, breaker=None,
                 sleep=time.sleep, rng=None):
        self._inner = inner
        self.name = getattr(inner, 'name', 'resilient')
        self._policy = policy or RetryPolicy()
        self._breaker = breaker
        self._sleep = sleep
        self._rng = rng

    def _call(self, _name, *args, **kwargs):
        inner_fn = getattr(self._inner, _name)
        return call_with_retry(lambda: inner_fn(*args, **kwargs), self._policy,
                               sleep=self._sleep, rng=self._rng,
                               breaker=self._breaker)

    # ---- 行情（公共）----
    def list_instruments(self) -> List[Instrument]:
        return self._call('list_instruments')

    def fetch_ohlcv(self, symbol: str, timeframe: str,
                    start_ms: int, end_ms: int) -> pd.DataFrame:
        return self._call('fetch_ohlcv', symbol, timeframe, start_ms, end_ms)

    def fetch_funding_history(self, symbol: str,
                             start_ms: int, end_ms: int) -> pd.DataFrame:
        return self._call('fetch_funding_history', symbol, start_ms, end_ms)

    def fetch_price(self, symbol: str) -> float:
        return self._call('fetch_price', symbol)

    # ---- 账户/交易（私有）----
    def fetch_balance(self) -> Balance:
        return self._call('fetch_balance')

    def fetch_positions(self, symbol: str) -> Position:
        return self._call('fetch_positions', symbol)

    def create_limit_order(self, symbol: str, side: str, price: float, size: float,
                           *, post_only: bool = False, reduce_only: bool = False,
                           client_oid: Optional[str] = None) -> Order:
        return self._call('create_limit_order', symbol, side, price, size,
                          post_only=post_only, reduce_only=reduce_only,
                          client_oid=client_oid)

    def create_market_order(self, symbol: str, side: str, size: float,
                            *, reduce_only: bool = False,
                            client_oid: Optional[str] = None) -> Order:
        return self._call('create_market_order', symbol, side, size,
                          reduce_only=reduce_only, client_oid=client_oid)

    def create_stop_order(self, symbol: str, side: str, size: float,
                          trigger_price: float, *, reduce_only: bool = True,
                          slippage: float = 0.15,
                          client_oid: Optional[str] = None) -> Order:
        return self._call('create_stop_order', symbol, side, size, trigger_price,
                          reduce_only=reduce_only, slippage=slippage,
                          client_oid=client_oid)

    def cancel_order(self, symbol: str, order_id: str) -> None:
        return self._call('cancel_order', symbol, order_id)

    def cancel_all(self, symbol: str) -> None:
        return self._call('cancel_all', symbol)

    def fetch_open_orders(self, symbol: str) -> List[Order]:
        return self._call('fetch_open_orders', symbol)

    def fetch_my_trades(self, symbol: str,
                        since_ms: Optional[int] = None) -> List[Trade]:
        return self._call('fetch_my_trades', symbol, since_ms=since_ms)

    def set_leverage(self, symbol: str, leverage: float) -> None:
        return self._call('set_leverage', symbol, leverage)

    def exchange_status(self) -> str:
        return self._call('exchange_status')

    def fetch_funding_payments(self, symbol: str,
                               since_ms: Optional[int] = None) -> List[FundingPayment]:
        return self._call('fetch_funding_payments', symbol, since_ms=since_ms)

    # ---- 可选：标记价 K线 ----
    def fetch_mark_ohlcv(self, symbol: str, timeframe: str,
                         start_ms: int, end_ms: int) -> pd.DataFrame:
        return self._call('fetch_mark_ohlcv', symbol, timeframe, start_ms, end_ms)

    def fetch_24h_quote_volumes(self) -> dict:
        return self._call('fetch_24h_quote_volumes')
