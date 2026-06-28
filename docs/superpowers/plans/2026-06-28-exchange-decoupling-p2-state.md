# 交易所解耦重构 P2 实现计划（外部托管状态层）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 `gridtrade/state/` 持久化状态层：SQLAlchemy Core 引擎无关的仓储（Repository）抽象，支持网格意图/订单/记账/历史记录的持久化，含 `(exchange,active_symbol)` 可移植部分唯一约束（一币种至多一个活跃网格）与 version 乐观锁，单测跑内存 SQLite、生产跑 Postgres。

**Architecture:** Repository 模式 + SQLAlchemy Core（非 ORM），引擎无关。`StateStore` 包一个 SQLAlchemy Engine（`in_memory()` 用 SQLite StaticPool 供测试；`from_url()` 供 Postgres 生产）。四张表 grids/grid_orders/grid_accounting/order_records。活跃唯一性用"可空 `active_symbol` 列 + UNIQUE(exchange,active_symbol)"实现（NULL 不互相冲突，关网即置 NULL 释放槽位），SQLite 与 Postgres 行为一致。乐观锁用 `UPDATE … WHERE id=? AND version=?` 的 rowcount 判定。

**Tech Stack:** Python 3.9、SQLAlchemy 2.0.51（Core）、psycopg2-binary 2.9.12（生产 Postgres 驱动）、内置 sqlite3（测试）、pytest。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（接口语义、SQLAlchemy 2.0 API、跨方言差异、本计划未写清的细节），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；SQLAlchemy 2.0.x（用 2.0 风格：`create_engine(url, future=True)`、`with engine.begin() as conn: conn.execute(stmt)`，不用已废弃的 `engine.execute`）。
- 单测一律针对内存 SQLite（`StateStore.in_memory()`，用 `StaticPool` + `check_same_thread=False` 让多次 `engine.begin()` 共享同一内存库）。**测试不得依赖外部 Postgres / docker / 网络。**
- 同一份仓储代码必须能在 Postgres 上运行（仅靠 URL 切换）；不得使用方言特有 SQL。活跃唯一性用可空 `active_symbol` 列方案（不可用 `postgresql_where` 之类方言部分索引）。
- 时间戳一律存 UTC 毫秒整数（`int(time.time()*1000)`）。
- 乐观锁：写操作用 `UPDATE … WHERE id=? AND version=?`，`result.rowcount==0` 视为版本陈旧 → 抛 `ConcurrencyError`。
- 状态机：网格状态 ∈ {PENDING,OPENING,ACTIVE,CLOSING,CLOSED,FAILED}；ACTIVE_STATES（占用币种槽位）= {PENDING,OPENING,ACTIVE,CLOSING}；非法状态跃迁抛 `StateError`。
- `gridtrade/state/` 不得 import 交易所库（ccxt 等）或 `gridtrade/core/`；它是独立的持久化层。
- 不修改 `account_0/`、`backtest/`、`gridtrade/core/`、`gridtrade/exchanges/`。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`（沿用既有 venv 与 conftest 时区固定）。

---

## 文件结构（本计划新建/修改）

```
gridtrade/state/
  __init__.py
  models.py        # MetaData + 4 张 Table + 状态常量/跃迁 + 数据类(Grid/GridOrder/Accounting/Record) + 异常 + now_ms
  store.py         # StateStore: engine 包装 + create_all/drop_all + in_memory()/from_url()
  grids.py         # GridRepository
  orders.py        # OrderRepository
  accounting.py    # AccountingRepository
  records.py       # RecordRepository
tests/state/
  __init__.py
  test_store_schema.py      # create_all + 活跃唯一约束 + NULL 去重（schema 级）
  test_grids.py
  test_orders.py
  test_accounting.py
  test_records.py
requirements.txt   # 追加 SQLAlchemy / psycopg2-binary
```

---

### Task 1: 依赖 + 状态层脚手架 + models + StateStore（schema 与唯一约束）

**Files:**
- Modify: `requirements.txt`
- Create: `gridtrade/state/__init__.py`, `gridtrade/state/models.py`, `gridtrade/state/store.py`
- Create: `tests/state/__init__.py`, `tests/state/test_store_schema.py`

**Interfaces:**
- Produces:
  - `gridtrade.state.models`: `metadata`（MetaData）；表对象 `grids, grid_orders, grid_accounting, order_records`；状态常量 `PENDING,OPENING,ACTIVE,CLOSING,CLOSED,FAILED`、`ACTIVE_STATES`、`TERMINAL_STATES`、`ALL_STATES`；`can_transition(src,dst)->bool`；异常 `ConcurrencyError`、`StateError`；`now_ms()->int`；数据类 `Grid, GridOrder, Accounting, Record`。
  - `gridtrade.state.store.StateStore`：`__init__(self, engine)`；classmethod `in_memory()->StateStore`；classmethod `from_url(url)->StateStore`；`create_all()`；`drop_all()`；属性 `engine`。

- [ ] **Step 1: 写 schema 失败测试**

Create `tests/state/__init__.py`（空）。

Create `tests/state/test_store_schema.py`:

```python
import sqlalchemy as sa
import pytest


def _store():
    from gridtrade.state.store import StateStore
    s = StateStore.in_memory()
    s.create_all()
    return s


def test_create_all_builds_tables():
    s = _store()
    insp = sa.inspect(s.engine)
    tables = set(insp.get_table_names())
    assert {'grids', 'grid_orders', 'grid_accounting', 'order_records'} <= tables


def test_active_symbol_unique_blocks_second_active():
    from gridtrade.state.models import grids
    s = _store()
    row = dict(id='g1', exchange='okx', symbol='BTC/USDT:USDT',
               active_symbol='BTC/USDT:USDT', offset=0, tag='t', status='ACTIVE',
               direction='neutral', created_at=1, updated_at=1, version=1)
    with s.engine.begin() as c:
        c.execute(sa.insert(grids), row)
    with pytest.raises(sa.exc.IntegrityError):
        with s.engine.begin() as c:
            c.execute(sa.insert(grids), dict(row, id='g2'))


def test_null_active_symbol_does_not_collide():
    from gridtrade.state.models import grids
    s = _store()
    base = dict(exchange='okx', symbol='BTC/USDT:USDT', active_symbol=None,
                offset=0, tag='t', status='CLOSED', direction='neutral',
                created_at=1, updated_at=1, version=1)
    with s.engine.begin() as c:
        c.execute(sa.insert(grids), dict(base, id='g3'))
        c.execute(sa.insert(grids), dict(base, id='g4'))
    with s.engine.begin() as c:
        n = c.execute(sa.select(sa.func.count()).select_from(grids)).scalar()
    assert n == 2


def test_can_transition_and_states():
    from gridtrade.state import models as m
    assert m.can_transition(m.PENDING, m.OPENING)
    assert m.can_transition(m.OPENING, m.ACTIVE)
    assert m.can_transition(m.ACTIVE, m.CLOSING)
    assert m.can_transition(m.CLOSING, m.CLOSED)
    assert not m.can_transition(m.ACTIVE, m.PENDING)
    assert not m.can_transition(m.CLOSED, m.ACTIVE)
    assert set(m.ACTIVE_STATES) == {m.PENDING, m.OPENING, m.ACTIVE, m.CLOSING}
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_store_schema.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.state.store`）。

- [ ] **Step 3: 写 models.py**

Create `gridtrade/state/__init__.py`（空）。

Create `gridtrade/state/models.py`:

```python
"""状态层数据模型：SQLAlchemy Core 表定义 + 状态机 + 数据类 + 异常。
引擎无关；不 import 交易所库或 gridtrade.core。时间戳一律 UTC 毫秒整数。
"""
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import (Column, Float, Index, Integer, MetaData, String, Table,
                        UniqueConstraint)

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
    Column('created_at', Integer, nullable=False),
    Column('updated_at', Integer, nullable=False),
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
    Column('created_at', Integer, nullable=False),
    Column('updated_at', Integer, nullable=False),
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
    Column('updated_at', Integer, nullable=False),
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
    Column('opened_at', Integer, nullable=True),
    Column('closed_at', Integer, nullable=True),
    Column('sz', Float, nullable=True),
    Column('total_pnl', Float, nullable=True),
    Column('pnl_ratio', Float, nullable=True),
    Column('exit_reason', String, nullable=True),
    Column('created_at', Integer, nullable=False),
    Index('ix_order_records_tag', 'tag'),
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
```

- [ ] **Step 4: 写 store.py**

Create `gridtrade/state/store.py`:

```python
"""StateStore：包装一个 SQLAlchemy Engine。
in_memory() 用 SQLite StaticPool（多次 begin() 共享同一内存库）供测试；
from_url() 供 Postgres 生产（如 postgresql+psycopg2://user:pw@host/db）。
"""
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from gridtrade.state.models import metadata


class StateStore:
    def __init__(self, engine):
        self.engine = engine

    @classmethod
    def from_url(cls, url: str) -> 'StateStore':
        return cls(create_engine(url, future=True))

    @classmethod
    def in_memory(cls) -> 'StateStore':
        engine = create_engine(
            'sqlite://', future=True,
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        return cls(engine)

    def create_all(self) -> None:
        metadata.create_all(self.engine)

    def drop_all(self) -> None:
        metadata.drop_all(self.engine)
```

- [ ] **Step 5: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_store_schema.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 6: 追加依赖并提交**

Edit `requirements.txt`：在 `pyarrow` 行附近追加两行：
```
SQLAlchemy>=2.0,<2.1
psycopg2-binary>=2.9,<3
```

```bash
git add requirements.txt gridtrade/state/__init__.py gridtrade/state/models.py gridtrade/state/store.py tests/state/__init__.py tests/state/test_store_schema.py
git commit -m "feat(state): add SQLAlchemy Core state schema + StateStore (sqlite/pg)"
```

---

### Task 2: GridRepository（CRUD + 活跃唯一 + 乐观锁 + 状态机）

**Files:**
- Create: `gridtrade/state/grids.py`
- Create: `tests/state/test_grids.py`

**Interfaces:**
- Consumes: `StateStore`（`.engine`）；`gridtrade.state.models`（grids 表、Grid 数据类、ACTIVE_STATES、TERMINAL_STATES、can_transition、ConcurrencyError、StateError、now_ms）。
- Produces: `gridtrade.state.grids.GridRepository`：
  - `__init__(self, store)`
  - `create(self, grid: Grid) -> Grid`：写入新网格。若 `grid.id` 为空则生成 uuid4 hex；`created_at/updated_at` 用 now_ms（若为 0）；`version=1`；`active_symbol = symbol if status in ACTIVE_STATES else None`。返回写回后的 Grid。
  - `get(self, grid_id: str) -> Optional[Grid]`
  - `get_active_by_symbol(self, exchange: str, symbol: str) -> Optional[Grid]`：返回该 (exchange,symbol) 当前活跃网格（active_symbol==symbol）或 None。
  - `list_active(self) -> List[Grid]`：所有 status ∈ ACTIVE_STATES 的网格。
  - `transition_status(self, grid_id: str, new_status: str, *, expected_version: int) -> Grid`：校验 can_transition（否则 StateError）；乐观锁更新（version 不匹配抛 ConcurrencyError）；进入 TERMINAL_STATES 时把 active_symbol 置 None（释放槽位）；version+1、updated_at=now_ms。返回更新后的 Grid。

- [ ] **Step 1: 写测试**

Create `tests/state/test_grids.py`:

```python
import pytest

from gridtrade.state.models import (Grid, ACTIVE, OPENING, CLOSED, CLOSING,
                                    PENDING, ConcurrencyError, StateError)


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.grids import GridRepository
    s = StateStore.in_memory()
    s.create_all()
    return GridRepository(s)


def _grid(**kw):
    base = dict(id='', exchange='okx', symbol='BTC/USDT:USDT', status=PENDING)
    base.update(kw)
    return Grid(**base)


def test_create_assigns_id_and_timestamps():
    repo = _repo()
    g = repo.create(_grid())
    assert g.id and g.created_at > 0 and g.updated_at > 0 and g.version == 1
    assert repo.get(g.id).symbol == 'BTC/USDT:USDT'


def test_get_active_by_symbol():
    repo = _repo()
    g = repo.create(_grid(status=ACTIVE))
    found = repo.get_active_by_symbol('okx', 'BTC/USDT:USDT')
    assert found is not None and found.id == g.id
    assert repo.get_active_by_symbol('okx', 'ETH/USDT:USDT') is None


def test_second_active_same_symbol_rejected():
    import sqlalchemy as sa
    repo = _repo()
    repo.create(_grid(status=ACTIVE))
    with pytest.raises(sa.exc.IntegrityError):
        repo.create(_grid(status=ACTIVE))


def test_transition_optimistic_lock_and_slot_release():
    repo = _repo()
    g = repo.create(_grid(status=OPENING))
    # 陈旧 version 抛 ConcurrencyError
    with pytest.raises(ConcurrencyError):
        repo.transition_status(g.id, ACTIVE, expected_version=999)
    g2 = repo.transition_status(g.id, ACTIVE, expected_version=g.version)
    assert g2.status == ACTIVE and g2.version == g.version + 1
    # 关到终态释放槽位，可再开同币种活跃网格
    repo.transition_status(g2.id, CLOSING, expected_version=g2.version)
    g4 = repo.get(g.id)
    repo.transition_status(g4.id, CLOSED, expected_version=g4.version)
    assert repo.get_active_by_symbol('okx', 'BTC/USDT:USDT') is None
    again = repo.create(_grid(status=ACTIVE))
    assert again.id != g.id


def test_illegal_transition_raises_state_error():
    repo = _repo()
    g = repo.create(_grid(status=ACTIVE))
    with pytest.raises(StateError):
        repo.transition_status(g.id, PENDING, expected_version=g.version)


def test_list_active_excludes_terminal():
    repo = _repo()
    a = repo.create(_grid(symbol='AAA/USDT:USDT', status=ACTIVE))
    b = repo.create(_grid(symbol='BBB/USDT:USDT', status=OPENING))
    c = repo.create(_grid(symbol='CCC/USDT:USDT', status=ACTIVE))
    repo.transition_status(c.id, CLOSING, expected_version=c.version)
    c2 = repo.get(c.id)
    repo.transition_status(c2.id, CLOSED, expected_version=c2.version)
    ids = {g.id for g in repo.list_active()}
    assert a.id in ids and b.id in ids and c.id not in ids
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_grids.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.state.grids`）。

- [ ] **Step 3: 写 grids.py**

Create `gridtrade/state/grids.py`:

```python
"""GridRepository：网格意图的持久化（活跃唯一 + 乐观锁 + 状态机）。"""
import uuid
from typing import List, Optional

from sqlalchemy import insert, select, update

from gridtrade.state.models import (ACTIVE_STATES, ConcurrencyError, Grid,
                                    StateError, TERMINAL_STATES, can_transition,
                                    grids, now_ms)

_FIELDS = ('id', 'exchange', 'symbol', 'status', 'offset', 'tag', 'direction',
           'entry_price', 'low_price', 'high_price', 'stop_low_price',
           'stop_high_price', 'grid_count', 'order_num', 'leverage', 'cap',
           'created_at', 'updated_at', 'version')


def _to_grid(row) -> Grid:
    m = row._mapping
    return Grid(**{f: m[f] for f in _FIELDS})


class GridRepository:
    def __init__(self, store):
        self.engine = store.engine

    def create(self, grid: Grid) -> Grid:
        gid = grid.id or uuid.uuid4().hex
        ts = now_ms()
        created = grid.created_at or ts
        updated = grid.updated_at or ts
        active_symbol = grid.symbol if grid.status in ACTIVE_STATES else None
        values = {f: getattr(grid, f) for f in _FIELDS}
        values.update(id=gid, created_at=created, updated_at=updated, version=1,
                      active_symbol=active_symbol)
        with self.engine.begin() as c:
            c.execute(insert(grids), values)
        return self.get(gid)

    def get(self, grid_id: str) -> Optional[Grid]:
        with self.engine.begin() as c:
            row = c.execute(select(grids).where(grids.c.id == grid_id)).first()
        return _to_grid(row) if row is not None else None

    def get_active_by_symbol(self, exchange: str, symbol: str) -> Optional[Grid]:
        with self.engine.begin() as c:
            row = c.execute(
                select(grids).where(grids.c.exchange == exchange,
                                    grids.c.active_symbol == symbol)
            ).first()
        return _to_grid(row) if row is not None else None

    def list_active(self) -> List[Grid]:
        with self.engine.begin() as c:
            rows = c.execute(
                select(grids).where(grids.c.status.in_(ACTIVE_STATES))
            ).all()
        return [_to_grid(r) for r in rows]

    def transition_status(self, grid_id: str, new_status: str, *,
                          expected_version: int) -> Grid:
        current = self.get(grid_id)
        if current is None:
            raise ConcurrencyError(f'grid {grid_id} not found')
        if not can_transition(current.status, new_status):
            raise StateError(f'illegal transition {current.status} -> {new_status}')
        active_symbol = None if new_status in TERMINAL_STATES else (
            current.symbol if new_status in ACTIVE_STATES else None)
        with self.engine.begin() as c:
            res = c.execute(
                update(grids)
                .where(grids.c.id == grid_id, grids.c.version == expected_version)
                .values(status=new_status, active_symbol=active_symbol,
                        version=expected_version + 1, updated_at=now_ms())
            )
            if res.rowcount == 0:
                raise ConcurrencyError(
                    f'stale version for grid {grid_id}: expected {expected_version}')
        return self.get(grid_id)
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_grids.py -v`
Expected: PASS（6 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/state/grids.py tests/state/test_grids.py
git commit -m "feat(state): GridRepository (active uniqueness, optimistic lock, state machine)"
```

---

### Task 3: OrderRepository（按 client_oid upsert + 按网格查询）

**Files:**
- Create: `gridtrade/state/orders.py`
- Create: `tests/state/test_orders.py`

**Interfaces:**
- Consumes: `StateStore`；`gridtrade.state.models`（grid_orders 表、GridOrder 数据类、now_ms）。
- Produces: `gridtrade.state.orders.OrderRepository`：
  - `__init__(self, store)`
  - `upsert(self, order: GridOrder) -> GridOrder`：按主键 client_oid 插入或更新（更新 exchange_order_id/side/price/size/status/updated_at；created_at 首次写入后保持不变）。返回写回后的 GridOrder。
  - `get(self, client_oid: str) -> Optional[GridOrder]`
  - `list_by_grid(self, grid_id: str) -> List[GridOrder]`：按 created_at 升序。
  - `list_open_by_grid(self, grid_id: str) -> List[GridOrder]`：仅 status=='open'。

- [ ] **Step 1: 写测试**

Create `tests/state/test_orders.py`:

```python
from gridtrade.state.models import GridOrder


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.orders import OrderRepository
    s = StateStore.in_memory()
    s.create_all()
    return OrderRepository(s)


def _order(**kw):
    base = dict(client_oid='g1:0', grid_id='g1', line_index=0, side='buy',
                price=100.0, size=1.0, status='open')
    base.update(kw)
    return GridOrder(**base)


def test_upsert_insert_then_get():
    repo = _repo()
    o = repo.upsert(_order())
    assert o.created_at > 0 and o.updated_at > 0
    got = repo.get('g1:0')
    assert got.grid_id == 'g1' and got.line_index == 0 and got.status == 'open'


def test_upsert_updates_status_preserves_created_at():
    repo = _repo()
    first = repo.upsert(_order())
    updated = repo.upsert(_order(status='closed', exchange_order_id='X7'))
    assert updated.status == 'closed' and updated.exchange_order_id == 'X7'
    assert updated.created_at == first.created_at
    assert updated.updated_at >= first.updated_at


def test_list_by_grid_and_open_filter():
    repo = _repo()
    repo.upsert(_order(client_oid='g1:0', line_index=0))
    repo.upsert(_order(client_oid='g1:1', line_index=1, side='sell', status='closed'))
    repo.upsert(_order(client_oid='g2:0', grid_id='g2', line_index=0))
    all_g1 = repo.list_by_grid('g1')
    assert {o.client_oid for o in all_g1} == {'g1:0', 'g1:1'}
    open_g1 = repo.list_open_by_grid('g1')
    assert {o.client_oid for o in open_g1} == {'g1:0'}


def test_get_missing_returns_none():
    assert _repo().get('nope') is None
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_orders.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.state.orders`）。

- [ ] **Step 3: 写 orders.py**

Create `gridtrade/state/orders.py`:

```python
"""OrderRepository：网格挂单的持久化（按 client_oid 主键 upsert）。"""
from typing import List, Optional

from sqlalchemy import insert, select, update

from gridtrade.state.models import GridOrder, grid_orders, now_ms

_FIELDS = ('client_oid', 'grid_id', 'line_index', 'exchange_order_id', 'side',
           'price', 'size', 'status', 'created_at', 'updated_at')


def _to_order(row) -> GridOrder:
    m = row._mapping
    return GridOrder(**{f: m[f] for f in _FIELDS})


class OrderRepository:
    def __init__(self, store):
        self.engine = store.engine

    def upsert(self, order: GridOrder) -> GridOrder:
        ts = now_ms()
        existing = self.get(order.client_oid)
        if existing is None:
            values = {f: getattr(order, f) for f in _FIELDS}
            values['created_at'] = order.created_at or ts
            values['updated_at'] = ts
            with self.engine.begin() as c:
                c.execute(insert(grid_orders), values)
        else:
            with self.engine.begin() as c:
                c.execute(
                    update(grid_orders)
                    .where(grid_orders.c.client_oid == order.client_oid)
                    .values(grid_id=order.grid_id, line_index=order.line_index,
                            exchange_order_id=order.exchange_order_id,
                            side=order.side, price=order.price, size=order.size,
                            status=order.status, updated_at=ts)
                )
        return self.get(order.client_oid)

    def get(self, client_oid: str) -> Optional[GridOrder]:
        with self.engine.begin() as c:
            row = c.execute(
                select(grid_orders).where(grid_orders.c.client_oid == client_oid)
            ).first()
        return _to_order(row) if row is not None else None

    def list_by_grid(self, grid_id: str) -> List[GridOrder]:
        with self.engine.begin() as c:
            rows = c.execute(
                select(grid_orders).where(grid_orders.c.grid_id == grid_id)
                .order_by(grid_orders.c.created_at)
            ).all()
        return [_to_order(r) for r in rows]

    def list_open_by_grid(self, grid_id: str) -> List[GridOrder]:
        return [o for o in self.list_by_grid(grid_id) if o.status == 'open']
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_orders.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/state/orders.py tests/state/test_orders.py
git commit -m "feat(state): OrderRepository (upsert by client_oid, by-grid queries)"
```

---

### Task 4: AccountingRepository（乐观锁记账 + 峰值收益）

**Files:**
- Create: `gridtrade/state/accounting.py`
- Create: `tests/state/test_accounting.py`

**Interfaces:**
- Consumes: `StateStore`；`gridtrade.state.models`（grid_accounting 表、Accounting 数据类、ConcurrencyError、now_ms）。
- Produces: `gridtrade.state.accounting.AccountingRepository`：
  - `__init__(self, store)`
  - `init(self, grid_id: str) -> Accounting`：为网格建零值记账行（version=1）；若已存在则返回现有。
  - `get(self, grid_id: str) -> Optional[Accounting]`
  - `save(self, acc: Accounting) -> Accounting`：乐观锁更新（`WHERE grid_id AND version=acc.version`，不匹配抛 ConcurrencyError）；version+1、updated_at=now_ms。返回更新后的 Accounting。
  - `bump_peak(self, grid_id: str, pnl_ratio: float) -> Accounting`：若 pnl_ratio 高于现存 pnl_ratio_max 则更新（用 save 的乐观锁，内部读-改-写，单次不命中重试一次）；否则原样返回。

- [ ] **Step 1: 写测试**

Create `tests/state/test_accounting.py`:

```python
import pytest

from gridtrade.state.models import Accounting, ConcurrencyError


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.accounting import AccountingRepository
    s = StateStore.in_memory()
    s.create_all()
    return AccountingRepository(s)


def test_init_creates_zero_row():
    repo = _repo()
    a = repo.init('g1')
    assert a.grid_id == 'g1' and a.realized_pnl == 0.0 and a.version == 1
    # 幂等：再次 init 返回现有
    assert repo.init('g1').version == 1


def test_save_optimistic_lock():
    repo = _repo()
    a = repo.init('g1')
    a.realized_pnl = 12.5
    a.net_position = 3.0
    saved = repo.save(a)
    assert saved.realized_pnl == 12.5 and saved.version == 2
    # 用陈旧 version 再保存应失败
    stale = Accounting(grid_id='g1', version=1)
    with pytest.raises(ConcurrencyError):
        repo.save(stale)


def test_bump_peak_only_increases():
    repo = _repo()
    repo.init('g1')
    a1 = repo.bump_peak('g1', 0.02)
    assert a1.pnl_ratio_max == 0.02
    a2 = repo.bump_peak('g1', 0.01)   # 更低，不更新
    assert a2.pnl_ratio_max == 0.02
    a3 = repo.bump_peak('g1', 0.05)   # 新高
    assert a3.pnl_ratio_max == 0.05
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_accounting.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.state.accounting`）。

- [ ] **Step 3: 写 accounting.py**

Create `gridtrade/state/accounting.py`:

```python
"""AccountingRepository：网格实时记账（乐观锁 + 峰值收益跟踪）。"""
from typing import Optional

from sqlalchemy import insert, select, update

from gridtrade.state.models import (Accounting, ConcurrencyError, grid_accounting,
                                    now_ms)

_FIELDS = ('grid_id', 'realized_pnl', 'fee_paid', 'funding_paid', 'net_position',
           'avg_price', 'pnl_ratio_max', 'updated_at', 'version')


def _to_acc(row) -> Accounting:
    m = row._mapping
    return Accounting(**{f: m[f] for f in _FIELDS})


class AccountingRepository:
    def __init__(self, store):
        self.engine = store.engine

    def init(self, grid_id: str) -> Accounting:
        existing = self.get(grid_id)
        if existing is not None:
            return existing
        with self.engine.begin() as c:
            c.execute(insert(grid_accounting), {
                'grid_id': grid_id, 'realized_pnl': 0.0, 'fee_paid': 0.0,
                'funding_paid': 0.0, 'net_position': 0.0, 'avg_price': 0.0,
                'pnl_ratio_max': 0.0, 'updated_at': now_ms(), 'version': 1,
            })
        return self.get(grid_id)

    def get(self, grid_id: str) -> Optional[Accounting]:
        with self.engine.begin() as c:
            row = c.execute(
                select(grid_accounting).where(grid_accounting.c.grid_id == grid_id)
            ).first()
        return _to_acc(row) if row is not None else None

    def save(self, acc: Accounting) -> Accounting:
        with self.engine.begin() as c:
            res = c.execute(
                update(grid_accounting)
                .where(grid_accounting.c.grid_id == acc.grid_id,
                       grid_accounting.c.version == acc.version)
                .values(realized_pnl=acc.realized_pnl, fee_paid=acc.fee_paid,
                        funding_paid=acc.funding_paid, net_position=acc.net_position,
                        avg_price=acc.avg_price, pnl_ratio_max=acc.pnl_ratio_max,
                        version=acc.version + 1, updated_at=now_ms())
            )
            if res.rowcount == 0:
                raise ConcurrencyError(
                    f'stale version for accounting {acc.grid_id}: {acc.version}')
        return self.get(acc.grid_id)

    def bump_peak(self, grid_id: str, pnl_ratio: float) -> Accounting:
        for _ in range(2):  # 读-改-写；并发陈旧时重试一次
            acc = self.get(grid_id)
            if acc is None:
                acc = self.init(grid_id)
            if pnl_ratio <= acc.pnl_ratio_max:
                return acc
            acc.pnl_ratio_max = pnl_ratio
            try:
                return self.save(acc)
            except ConcurrencyError:
                continue
        return self.get(grid_id)
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_accounting.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/state/accounting.py tests/state/test_accounting.py
git commit -m "feat(state): AccountingRepository (optimistic lock, peak pnl tracking)"
```

---

### Task 5: RecordRepository（历史成交记录）+ 全套回归

**Files:**
- Create: `gridtrade/state/records.py`
- Create: `tests/state/test_records.py`

**Interfaces:**
- Consumes: `StateStore`；`gridtrade.state.models`（order_records 表、Record 数据类、now_ms）。
- Produces: `gridtrade.state.records.RecordRepository`：
  - `__init__(self, store)`
  - `add(self, record: Record) -> Record`：插入一条历史记录（id 为空则生成 uuid4 hex；created_at 用 now_ms）。返回写回后的 Record。
  - `get(self, record_id: str) -> Optional[Record]`
  - `list_by_tag(self, tag: str) -> List[Record]`：按 created_at 升序。
  - `list_by_grid(self, grid_id: str) -> List[Record]`

- [ ] **Step 1: 写测试**

Create `tests/state/test_records.py`:

```python
from gridtrade.state.models import Record


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.records import RecordRepository
    s = StateStore.in_memory()
    s.create_all()
    return RecordRepository(s)


def _rec(**kw):
    base = dict(id='', exchange='okx', symbol='BTC/USDT:USDT', tag='acc0at0')
    base.update(kw)
    return Record(**base)


def test_add_assigns_id_and_created_at():
    repo = _repo()
    r = repo.add(_rec(total_pnl=5.0, pnl_ratio=0.01, exit_reason='固定止损'))
    assert r.id and r.created_at > 0
    got = repo.get(r.id)
    assert got.total_pnl == 5.0 and got.exit_reason == '固定止损'


def test_list_by_tag_and_grid():
    repo = _repo()
    repo.add(_rec(tag='acc0at0', grid_id='g1'))
    repo.add(_rec(tag='acc0at0', grid_id='g2'))
    repo.add(_rec(tag='acc0at1', grid_id='g3'))
    assert len(repo.list_by_tag('acc0at0')) == 2
    assert len(repo.list_by_tag('acc0at1')) == 1
    assert {r.grid_id for r in repo.list_by_grid('g1')} == {'g1'}


def test_get_missing_returns_none():
    assert _repo().get('nope') is None
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_records.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.state.records`）。

- [ ] **Step 3: 写 records.py**

Create `gridtrade/state/records.py`:

```python
"""RecordRepository：历史成交/关仓记录（替代 orderInfo.pkl / gridResult.csv）。"""
import uuid
from typing import List, Optional

from sqlalchemy import insert, select

from gridtrade.state.models import Record, now_ms, order_records

_FIELDS = ('id', 'grid_id', 'exchange', 'symbol', 'tag', 'offset', 'opened_at',
           'closed_at', 'sz', 'total_pnl', 'pnl_ratio', 'exit_reason', 'created_at')


def _to_record(row) -> Record:
    m = row._mapping
    return Record(**{f: m[f] for f in _FIELDS})


class RecordRepository:
    def __init__(self, store):
        self.engine = store.engine

    def add(self, record: Record) -> Record:
        rid = record.id or uuid.uuid4().hex
        values = {f: getattr(record, f) for f in _FIELDS}
        values['id'] = rid
        values['created_at'] = record.created_at or now_ms()
        with self.engine.begin() as c:
            c.execute(insert(order_records), values)
        return self.get(rid)

    def get(self, record_id: str) -> Optional[Record]:
        with self.engine.begin() as c:
            row = c.execute(
                select(order_records).where(order_records.c.id == record_id)
            ).first()
        return _to_record(row) if row is not None else None

    def list_by_tag(self, tag: str) -> List[Record]:
        with self.engine.begin() as c:
            rows = c.execute(
                select(order_records).where(order_records.c.tag == tag)
                .order_by(order_records.c.created_at)
            ).all()
        return [_to_record(r) for r in rows]

    def list_by_grid(self, grid_id: str) -> List[Record]:
        with self.engine.begin() as c:
            rows = c.execute(
                select(order_records).where(order_records.c.grid_id == grid_id)
                .order_by(order_records.c.created_at)
            ).all()
        return [_to_record(r) for r in rows]
```

- [ ] **Step 4: 运行确认通过 + 全套回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_records.py -v`
Expected: PASS（3 passed）。

Run（全仓回归，P0+P1+P2 全绿）: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全 PASS（既有 35 + 本计划新增 ≈ 20 个用例）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/state/records.py tests/state/test_records.py
git commit -m "feat(state): RecordRepository (history records replacing pkl/csv)"
```

---

## 完成判定（P2）

- `pytest -q` 全绿：状态层 schema/uniqueness、GridRepository（活跃唯一+乐观锁+状态机）、OrderRepository、AccountingRepository（峰值）、RecordRepository。
- `gridtrade/state/` 不 import 交易所库或 `gridtrade/core/`（`grep -rnE "ccxt|hyperliquid|gridtrade.core" gridtrade/state` 无匹配）。
- 测试全程内存 SQLite，无外部 Postgres/网络依赖。
- `requirements.txt` 含 SQLAlchemy / psycopg2-binary 版本钉死。

## 后续（不在本计划内）

P3 执行器（GridExecutor 状态机 + live_equity 记账 + reconciler 自愈，消费本状态层）、P4 运行时（triggers/gates/manager + scheduler/monitor + fly.io，Postgres advisory lock 做 leader 选举）、P5 回测数据层（datasource + 泛化 prewarm + HL 验证，含 quote_volume 真实成交额映射 carry-forward）、P6 加固、P7 同币种多网格。
