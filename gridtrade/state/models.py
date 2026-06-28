"""状态层数据模型：SQLAlchemy Core 表定义 + 状态机 + 数据类 + 异常。
引擎无关；不 import 交易所库或 gridtrade.core。时间戳一律 UTC 毫秒整数。
"""
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import (BigInteger, Column, Float, Index, Integer, MetaData,
                        String, Table, UniqueConstraint)

metadata = MetaData()

# ---- 网格生命周期状态 ----
PENDING = 'PENDING'
OPENING = 'OPENING'
ACTIVE = 'ACTIVE'
CLOSING = 'CLOSING'
CLOSED = 'CLOSED'
FAILED = 'FAILED'

# 占用币种槽位（active_symbol 非空）的状态
ACTIVE_STATES = (PENDING, OPENING, ACTIVE, CLOSING)
TERMINAL_STATES = (CLOSED, FAILED)
ALL_STATES = (PENDING, OPENING, ACTIVE, CLOSING, CLOSED, FAILED)

_TRANSITIONS = {
    PENDING: {OPENING, FAILED, CLOSED},
    OPENING: {ACTIVE, CLOSING, FAILED},
    ACTIVE: {CLOSING, FAILED},
    CLOSING: {CLOSED, FAILED},
    CLOSED: set(),
    FAILED: set(),
}


def can_transition(src: str, dst: str) -> bool:
    return dst in _TRANSITIONS.get(src, set())


def now_ms() -> int:
    return int(time.time() * 1000)


class ConcurrencyError(Exception):
    """乐观锁写入未命中预期 version（陈旧写）。"""


class StateError(Exception):
    """非法的网格状态跃迁。"""


# ---- 表定义 ----
grids = Table(
    'grids', metadata,
    Column('id', String, primary_key=True),
    Column('exchange', String, nullable=False),
    Column('symbol', String, nullable=False),
    # = symbol 当状态属于 ACTIVE_STATES；否则 NULL。UNIQUE(exchange,active_symbol)
    # 借 NULL 互不冲突实现"一币种至多一个活跃网格"的可移植部分唯一约束。
    Column('active_symbol', String, nullable=True),
    Column('offset', Integer, nullable=False, default=0),
    Column('tag', String, nullable=False, default=''),
    Column('status', String, nullable=False),
    Column('direction', String, nullable=False, default='neutral'),
    Column('entry_price', Float, nullable=True),
    Column('low_price', Float, nullable=True),
    Column('high_price', Float, nullable=True),
    Column('stop_low_price', Float, nullable=True),
    Column('stop_high_price', Float, nullable=True),
    Column('grid_count', Integer, nullable=True),
    Column('order_num', Float, nullable=True),
    Column('leverage', Float, nullable=True),
    Column('cap', Float, nullable=True),
    Column('created_at', BigInteger, nullable=False),
    Column('updated_at', BigInteger, nullable=False),
    Column('version', Integer, nullable=False, default=1),
    UniqueConstraint('exchange', 'active_symbol', name='uq_grids_active'),
)

grid_orders = Table(
    'grid_orders', metadata,
    Column('client_oid', String, primary_key=True),
    Column('grid_id', String, nullable=False),
    Column('line_index', Integer, nullable=False),
    Column('exchange_order_id', String, nullable=True),
    Column('side', String, nullable=False),
    Column('price', Float, nullable=False),
    Column('size', Float, nullable=False),
    Column('status', String, nullable=False),  # open/closed/canceled
    Column('created_at', BigInteger, nullable=False),
    Column('updated_at', BigInteger, nullable=False),
    Index('ix_grid_orders_grid', 'grid_id'),
)

grid_accounting = Table(
    'grid_accounting', metadata,
    Column('grid_id', String, primary_key=True),
    Column('realized_pnl', Float, nullable=False, default=0.0),
    Column('fee_paid', Float, nullable=False, default=0.0),
    Column('funding_paid', Float, nullable=False, default=0.0),
    Column('net_position', Float, nullable=False, default=0.0),
    Column('avg_price', Float, nullable=False, default=0.0),
    Column('pnl_ratio_max', Float, nullable=False, default=0.0),
    Column('funding_cursor', BigInteger, nullable=False, default=0),
    Column('updated_at', BigInteger, nullable=False),
    Column('version', Integer, nullable=False, default=1),
)

order_records = Table(
    'order_records', metadata,
    Column('id', String, primary_key=True),
    Column('grid_id', String, nullable=True),
    Column('exchange', String, nullable=False),
    Column('symbol', String, nullable=False),
    Column('tag', String, nullable=False, default=''),
    Column('offset', Integer, nullable=True),
    Column('opened_at', BigInteger, nullable=True),
    Column('closed_at', BigInteger, nullable=True),
    Column('sz', Float, nullable=True),
    Column('total_pnl', Float, nullable=True),
    Column('pnl_ratio', Float, nullable=True),
    Column('exit_reason', String, nullable=True),
    Column('created_at', BigInteger, nullable=False),
    Index('ix_order_records_tag', 'tag'),
)

grid_fills = Table(
    'grid_fills', metadata,
    Column('trade_id', String, primary_key=True),
    Column('grid_id', String, nullable=False),
    Column('line_index', Integer, nullable=False),
    Column('side', String, nullable=False),
    Column('price', Float, nullable=False),
    Column('size', Float, nullable=False),
    Column('ts', BigInteger, nullable=False),
    Column('created_at', BigInteger, nullable=False),
    Index('ix_grid_fills_grid', 'grid_id'),
)

heartbeats = Table(
    'heartbeats', metadata,
    Column('machine', String, primary_key=True),
    Column('last_beat_ts', BigInteger, nullable=False),
)


# ---- 数据类（仓储层入参/出参）----
@dataclass
class Grid:
    id: str
    exchange: str
    symbol: str
    status: str
    offset: int = 0
    tag: str = ''
    direction: str = 'neutral'
    entry_price: Optional[float] = None
    low_price: Optional[float] = None
    high_price: Optional[float] = None
    stop_low_price: Optional[float] = None
    stop_high_price: Optional[float] = None
    grid_count: Optional[int] = None
    order_num: Optional[float] = None
    leverage: Optional[float] = None
    cap: Optional[float] = None
    created_at: int = 0
    updated_at: int = 0
    version: int = 1


@dataclass
class GridOrder:
    client_oid: str
    grid_id: str
    line_index: int
    side: str
    price: float
    size: float
    status: str = 'open'
    exchange_order_id: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0


@dataclass
class Accounting:
    grid_id: str
    realized_pnl: float = 0.0
    fee_paid: float = 0.0
    funding_paid: float = 0.0
    net_position: float = 0.0
    avg_price: float = 0.0
    pnl_ratio_max: float = 0.0
    funding_cursor: int = 0
    updated_at: int = 0
    version: int = 1


@dataclass
class Record:
    id: str
    exchange: str
    symbol: str
    tag: str = ''
    grid_id: Optional[str] = None
    offset: Optional[int] = None
    opened_at: Optional[int] = None
    closed_at: Optional[int] = None
    sz: Optional[float] = None
    total_pnl: Optional[float] = None
    pnl_ratio: Optional[float] = None
    exit_reason: Optional[str] = None
    created_at: int = 0


@dataclass
class Fill:
    trade_id: str
    grid_id: str
    line_index: int
    side: str
    price: float
    size: float
    ts: int
    created_at: int = 0


@dataclass
class Heartbeat:
    machine: str
    last_beat_ts: int
