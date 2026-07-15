"""ResilientAdapter：把 P4f 健壮性（退避重试 + 熔断）包到内层 ExchangeAdapter 每个调用。

execution/runtime 拿到本适配器即天然健壮（需求 1）。重试安全靠 client_oid 幂等。

并发（monitor per-grid 并行化）：
- 电路按类别拆三路（market_read / account_read / trade_write），单端点故障只熔断
  所属类别，不再拖垮全局（7-02 fetch_my_trades 500 事故形态）。传 breaker= 保持
  单路旧语义（互斥，二选一）。
- 写方法全局串行：HL 按钱包要求 nonce 递增，并发写会撞 nonce。锁包**单次尝试**
  而非整个重试循环——退避 sleep 期间释放，不让一个重试中的写卡住全部写。
"""
import threading
import time
from typing import List, Optional

import pandas as pd

from gridtrade.exchanges.base import (Balance, ExchangeAdapter, FundingPayment,
                                      Instrument, Order, Position, Trade)
from gridtrade.exchanges.resilience import CircuitBreaker, RetryPolicy, call_with_retry

# 写方法穷举（base 接口审计）：漏一个 = nonce 竞态。新增写接口必须同步进这张表。
WRITE_METHODS = frozenset({
    'create_limit_order', 'create_market_order', 'create_stop_order',
    'cancel_order', 'cancel_all', 'set_leverage',
})
ACCOUNT_READ_METHODS = frozenset({
    'fetch_balance', 'fetch_positions', 'fetch_my_trades',
    'fetch_open_orders', 'fetch_funding_payments',
    'fetch_my_trades_all', 'fetch_open_orders_all', 'fetch_positions_all',
    'fetch_funding_payments_all', 'order_status',   # fetch_prices_all 不列 = market_read
})
CATEGORIES = ('market_read', 'account_read', 'trade_write')


def category_of(method_name: str) -> str:
    if method_name in WRITE_METHODS:
        return 'trade_write'
    if method_name in ACCOUNT_READ_METHODS:
        return 'account_read'
    return 'market_read'


def default_breakers() -> dict:
    return {c: CircuitBreaker() for c in CATEGORIES}


class ResilientAdapter(ExchangeAdapter):
    def __init__(self, inner, *, policy=None, breaker=None, breakers=None,
                 sleep=time.sleep, rng=None):
        if breaker is not None and breakers is not None:
            raise ValueError('breaker（单路）与 breakers（按类别）互斥，二选一')
        self._inner = inner
        self.name = getattr(inner, 'name', 'resilient')
        self._policy = policy or RetryPolicy()
        if breakers is not None:
            self._breakers = dict(breakers)
        elif breaker is not None:
            self._breakers = {c: breaker for c in CATEGORIES}   # 旧语义：单路共享
        else:
            self._breakers = None
        self._write_lock = threading.Lock()
        self._sleep = sleep
        self._rng = rng

    def _call(self, _name, *args, **kwargs):
        inner_fn = getattr(self._inner, _name)
        if _name in WRITE_METHODS:
            def attempt():
                with self._write_lock:      # 只锁单次尝试；退避 sleep 在锁外
                    return inner_fn(*args, **kwargs)
        else:
            def attempt():
                return inner_fn(*args, **kwargs)
        breaker = self._breakers.get(category_of(_name)) if self._breakers else None
        return call_with_retry(attempt, self._policy,
                               sleep=self._sleep, rng=self._rng,
                               breaker=breaker)

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

    def fetch_max_leverages(self) -> dict:
        # 必须显式代理:基类默认 {} 会把 lev_caps 静默吸掉(fail-open 掩盖,
        # 2026-07-12 mainnet 实证 VVV maxlev=3 开出双格)。内层有实例缓存,零额外请求。
        return self._call('fetch_max_leverages')

    def fetch_leverage_tiers(self, symbol: str) -> list:
        # 必须显式代理:基类默认 [] 会把档位表静默吸掉(同 fetch_max_leverages 教训,fail-open 掩盖)
        # → open 设杠杆永不生效。内层有实例缓存,零额外请求。
        return self._call('fetch_leverage_tiers', symbol)

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

    def quantize_amount(self, symbol, amount):
        """本地精度换算,无网络——直通 inner,不套重试/熔断。
        必须显式转发:本类逐方法转发无 __getattr__,漏转会静默落到基类恒等默认,
        量化修复在线上失效(2026-07-12 mainnet 核验实证,memory quantized-size-fallback-bug)。"""
        return self._inner.quantize_amount(symbol, amount)

    def assert_account_mode(self):
        """启动一次性断言，直通 inner（不套重试/熔断——失败须原样抛出拒绝起跑）。"""
        return self._inner.assert_account_mode()

    def order_status(self, symbol, order_id):
        return self._call('order_status', symbol, order_id)

    # ---- 账户级批量读（monitor 快照）----
    def fetch_my_trades_all(self, symbols, since_ms=None):
        return self._call('fetch_my_trades_all', symbols, since_ms=since_ms)

    def fetch_open_orders_all(self, symbols):
        return self._call('fetch_open_orders_all', symbols)

    def fetch_positions_all(self, symbols):
        return self._call('fetch_positions_all', symbols)

    def fetch_prices_all(self, symbols):
        return self._call('fetch_prices_all', symbols)

    def fetch_funding_payments_all(self, symbols, since_ms=None):
        return self._call('fetch_funding_payments_all', symbols, since_ms=since_ms)

    # ---- 可选：标记价 K线 ----
    def fetch_mark_ohlcv(self, symbol: str, timeframe: str,
                         start_ms: int, end_ms: int) -> pd.DataFrame:
        return self._call('fetch_mark_ohlcv', symbol, timeframe, start_ms, end_ms)

    def fetch_24h_quote_volumes(self) -> dict:
        return self._call('fetch_24h_quote_volumes')
