"""交易所抽象层（Ports & Adapters 的端口）。
规范符号 = ccxt 统一符号，永续如 'BASE/QUOTE:QUOTE'，其中 QUOTE 如实反映各所结算币
（OKX→USDT 即 'BTC/USDT:USDT'、HL→USDC 即 'BTC/USDC:USDC'）；由各适配器的
`quote_currency` 驱动（可经 QUOTE_CURRENCY config 覆写）。各所原生格式仅在各自适配器内部映射。
core 视符号为不透明字符串 ID，不解析其 quote 段。
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
    min_cost: float = 0.0  # 单笔最小名义额（币安 MIN_NOTIONAL；0=交易所无此约束/未知）


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

    def assert_account_mode(self) -> None:
        """启动断言：账户模式满足引擎假设（净仓语义/单币保证金）。默认无约束。
        monitor/scheduler 启动时调用一次；不满足抛 RuntimeError 拒绝起跑。"""
        return None

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
    def fetch_balance(self) -> Balance:
        """账户权益快照（quote_currency 计价）。"""

    @abstractmethod
    def fetch_positions(self, symbol: str) -> Position: ...

    def quantize_amount(self, symbol: str, amount: float) -> float:
        """交易所数量精度量化。默认原样返回（测试桩/无精度表交易所 fail-open）；
        ccxt 适配器覆写为真实精度表。下单量必须先经它（memory quantized-size-fallback-bug：
        HL create 响应不带数量，存储量依赖回传会退化为原始值 → 吃满判定永假）。"""
        return float(amount)

    def quantize_price(self, symbol: str, price: float) -> float:
        """交易所价格精度量化（tickSize）。默认原样返回（测试桩 fail-open）；ccxt 覆写为真实
        精度表。挂单价/触发价必须先经它——等比网格几何价 round(8) 常超粗 tickSize（testnet KITE
        tickSize=1e-05 实证 11/11 价超精度 → 限价单 -1111 拒 → 开格零挂单卡 OPENING）。"""
        return float(price)

    @abstractmethod
    def create_limit_order(self, symbol: str, side: str, price: float, size: float,
                           *, post_only: bool = False, reduce_only: bool = False,
                           client_oid: Optional[str] = None) -> Order: ...

    @abstractmethod
    def create_market_order(self, symbol: str, side: str, size: float,
                            *, reduce_only: bool = False,
                            client_oid: Optional[str] = None) -> Order: ...

    @abstractmethod
    def create_stop_order(self, symbol: str, side: str, size: float,
                          trigger_price: float, *,
                          reduce_only: bool = True, slippage: float = 0.15,
                          client_oid: Optional[str] = None) -> Order:
        """交易所原生触发市价单（灾难保险丝）。trigger_price=触发价；触发后市价成交，
        成交底线 = trigger_price×(1∓slippage)；reduce_only 默认 True。"""
        ...

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

    # ---- 可选：单订单状态（fuse 三态判定用；默认 unknown=调用方走 fills 后备）----
    def order_status(self, symbol: str, order_id: str) -> str:
        """'open'/'filled'/'canceled'/'unknown'。"""
        return 'unknown'

    # ---- 可选：最大杠杆(杠杆感知并发上限用;spec 2026-07-11-symbol-desk 组件四)----
    def fetch_max_leverages(self) -> dict:
        """{canonical symbol: maxLeverage}。默认空 dict(调用方 fail-open 跳过分级)。"""
        return {}

    def max_leverage(self, symbol: str):
        """单币 maxLeverage;None=未知(cap_for 退化为无杠杆感知)。"""
        return self.fetch_max_leverages().get(symbol)

    # ---- 可选：24h 成交额（用于流动性地板；默认空=上层跳过过滤）----
    def fetch_24h_quote_volumes(self) -> dict:
        """{canonical symbol: 24h 计价币成交额}。默认空 dict（无数据 → resolve_live_universe fail-open 跳过）。"""
        return {}

    # ---- 账户级批量读（monitor 快照唯一读取口，spec 2026-07-14 §四）----
    # 契约：返回调用时刻的最新已知状态（只读幂等，不要求强一致）；键/symbol 一律
    # canonical；列表按 ts 升序；实现不得让上层感知分页游标/权重/调用时序。
    # 未来 WsFeedAdapter 以内存镜像覆写本组方法即可无感升级（契约测试
    # tests/exchanges/test_snapshot_contract.py 为开发基准）。默认逐 symbol 合成。
    def fetch_my_trades_all(self, symbols, since_ms: Optional[int] = None) -> List[Trade]:
        """指定 symbols 的成交流水快照，ts 升序。"""
        out: List[Trade] = []
        for s in symbols:
            out.extend(self.fetch_my_trades(s, since_ms=since_ms))
        out.sort(key=lambda t: t.ts)
        return out

    def fetch_open_orders_all(self, symbols) -> List[Order]:
        """指定 symbols 的当前挂单快照（只含请求的 symbols）。"""
        out: List[Order] = []
        for s in symbols:
            out.extend(self.fetch_open_orders(s))
        return out

    def fetch_positions_all(self, symbols) -> dict:
        """{canonical: 带符号净仓}；无持仓可缺省（调用方按 0 处理）。"""
        return {s: float(self.fetch_positions(s).net_size) for s in symbols}

    def fetch_prices_all(self, symbols) -> dict:
        """{canonical: 最新价 float}。"""
        return {s: float(self.fetch_price(s)) for s in symbols}

    def fetch_funding_payments_all(self, symbols, since_ms: Optional[int] = None) -> dict:
        """{canonical: [FundingPayment]}，各列表 ts 升序，支付为正。"""
        return {s: self.fetch_funding_payments(s, since_ms=since_ms) for s in symbols}
