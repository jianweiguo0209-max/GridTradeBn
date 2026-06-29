"""交易所抽象层（Ports & Adapters 的端口）。
规范符号 = ccxt 统一符号，永续如 'BTC/USDT:USDT'。各所原生格式仅在各自适配器内部映射。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

CANDLE_COLS = ['symbol', 'candle_begin_time', 'open', 'high', 'low',
               'close', 'vol', 'volCcy', 'quote_volume']
FUNDING_COLS = ['ts', 'symbol', 'fundingRate', 'realizedRate']


@dataclass
class Instrument:
    symbol: str
    tick: float
    lot: float
    min_size: float
    state: str
    list_ts: int  # 上市时间，毫秒


@dataclass
class Balance:
    equity: float
    cash: float


@dataclass
class Position:
    symbol: str
    net_size: float   # 带符号：+多 / -空
    avg_price: float


@dataclass
class Order:
    id: str
    client_oid: str
    symbol: str
    side: str         # 'buy' / 'sell'
    price: float
    size: float
    filled: float
    status: str       # 'open' / 'closed' / 'canceled'
    reduce_only: bool


@dataclass
class Trade:
    id: str
    client_oid: str
    symbol: str
    side: str
    price: float
    size: float
    fee: float
    ts: int           # 毫秒
    order_id: Optional[str] = None   # 成交所属交易所订单号（fill→line 按它匹配）


@dataclass
class FundingPayment:
    ts: int       # 毫秒
    amount: float  # >0 表示支付（净值下降）


class ExchangeAdapter(ABC):
    """所有交易所适配器的统一端口。规范符号入参，统一 schema 出参。"""

    name: str = 'base'

    def encode_cloid(self, client_oid):
        """下单时发给交易所的 client order id。默认原样；返回 None=省略（如 HL）。"""
        return client_oid

    # ---- 行情（公共）----
    @abstractmethod
    def list_instruments(self) -> List[Instrument]: ...

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str,
                    start_ms: int, end_ms: int) -> pd.DataFrame:
        """返回列为 CANDLE_COLS、按 candle_begin_time 升序的 DataFrame。"""

    @abstractmethod
    def fetch_funding_history(self, symbol: str,
                             start_ms: int, end_ms: int) -> pd.DataFrame:
        """返回列为 FUNDING_COLS、按 ts 升序的 DataFrame。"""

    @abstractmethod
    def fetch_price(self, symbol: str) -> float: ...

    # ---- 账户/交易（私有）----
    @abstractmethod
    def fetch_balance(self) -> Balance: ...

    @abstractmethod
    def fetch_positions(self, symbol: str) -> Position: ...

    @abstractmethod
    def create_limit_order(self, symbol: str, side: str, price: float, size: float,
                           *, post_only: bool = False, reduce_only: bool = False,
                           client_oid: Optional[str] = None) -> Order: ...

    @abstractmethod
    def create_market_order(self, symbol: str, side: str, size: float,
                            *, reduce_only: bool = False,
                            client_oid: Optional[str] = None) -> Order: ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str) -> None: ...

    @abstractmethod
    def cancel_all(self, symbol: str) -> None: ...

    @abstractmethod
    def fetch_open_orders(self, symbol: str) -> List[Order]: ...

    @abstractmethod
    def fetch_my_trades(self, symbol: str,
                        since_ms: Optional[int] = None) -> List[Trade]: ...

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: float) -> None: ...

    @abstractmethod
    def exchange_status(self) -> str:
        """'ok' 或 'maintenance'。"""

    @abstractmethod
    def fetch_funding_payments(self, symbol: str,
                               since_ms: Optional[int] = None) -> List[FundingPayment]:
        """指定 symbol 的资金费扣款流水（只含本币种）；amount>0 表示支付。按 ts 升序。"""

    # ---- 可选：标记价 K线（默认未实现）----
    def fetch_mark_ohlcv(self, symbol: str, timeframe: str,
                         start_ms: int, end_ms: int) -> pd.DataFrame:
        raise NotImplementedError
