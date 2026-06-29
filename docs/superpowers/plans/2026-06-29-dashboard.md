# Dashboard (第一期只读监控) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 GridTradeGP 加一个只读 Web Dashboard（fly.io 第三个 `web` 进程），让单人运维一眼看清系统健康、活跃网格、单网格明细、历史战绩。

**Architecture:** FastAPI + Jinja2 + HTMX 服务端渲染，独立 `web` process group 与 `monitor`/`scheduler` 同镜像、读同一个 Fly Postgres。新增 `gridtrade/dashboard/` 子包：`queries.py`（只读查询层，复用现有 6 个仓储 + 直读表做聚合）、`auth.py`（登录会话 + 失败锁定）、`formatting.py`（Jinja filter）、`app.py`（FastAPI 应用工厂）；`runtime/web.py` 为进程入口。web 进程除只读行情（`fetch_price`/`fetch_balance`）外**绝不下单、绝不改状态**。

**Tech Stack:** Python 3.9 / FastAPI / uvicorn / Jinja2 / HTMX（vendored）/ SQLAlchemy 2.0 Core / pytest（双后端 fixture）。

## Global Constraints

- Python 3.9（仓库 `.venv`；pandas 1.3.5 / SQLAlchemy 2.0）。
- 测试命令：`TZ=Asia/Shanghai .venv/bin/python -m pytest`。
- 所有 DB 测试用 `tests/conftest.py` 的 `store` fixture（默认内存 SQLite，`TEST_DATABASE_URL` 有值走 PG）。
- `gridtrade/core/` 不依赖交易所库这一不变量不受本计划影响；dashboard 子包可 import 交易所 base 类型与 state 仓储，但**不得**调用任何写交易所/写状态的方法。
- 时间戳一律 UTC 毫秒整数；用 `gridtrade.state.models.now_ms`。
- 现价取自 adapter `fetch_price(symbol) -> float`；余额取自 `fetch_balance() -> Balance(equity, cash)`。
- 鉴权必须登录式：用户名+密码 → 会话 cookie；失败计数达 5 次锁定 ≥ 3600 秒。仅标准库实现哈希与签名，不引入 `passlib`/`itsdangerous`。
- 新依赖仅限：`fastapi`、`uvicorn[standard]`、`jinja2`（均兼容 py3.9）。HTMX 单文件 vendored 进 `static/`，不走 CDN。

---

### Task 1: 只读查询层 — 系统健康（HealthDTO）

**Files:**
- Create: `gridtrade/dashboard/__init__.py`
- Create: `gridtrade/dashboard/queries.py`
- Test: `tests/dashboard/__init__.py`
- Test: `tests/dashboard/test_queries_health.py`

**Interfaces:**
- Consumes: `gridtrade.state.heartbeats.HeartbeatRepository(store)` → `.list_all() -> List[Heartbeat(machine, last_beat_ts)]`；`gridtrade.state.models.now_ms`；adapter `fetch_balance() -> Balance(equity, cash)`；`gridtrade.runtime.introspect.adapter_endpoint(adapter) -> str`。
- Produces:
  - `@dataclass MachineHealth(machine: str, last_beat_ts: int, age_sec: float, stale: bool)`
  - `@dataclass HealthDTO(machines: List[MachineHealth], endpoint: str, equity: Optional[float], cash: Optional[float], balance_error: Optional[str], db_ok: bool)`
  - `def build_health(store, adapter, *, now_ms_fn=now_ms, stale_threshold_sec: float = 30.0) -> HealthDTO`

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_queries_health.py
from gridtrade.dashboard.queries import build_health
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.exchanges.base import Balance


class _FakeAdapter:
    def __init__(self, equity=499.0, cash=400.0, raise_balance=False):
        self._b = Balance(equity=equity, cash=cash)
        self._raise = raise_balance

    def fetch_balance(self):
        if self._raise:
            raise RuntimeError("network down")
        return self._b


def test_health_marks_stale_machine_and_reads_balance(store):
    hb = HeartbeatRepository(store)
    hb.beat('monitor', ts=1_000_000)
    hb.beat('scheduler', ts=1_000_000)

    # now = 1_000_000 + 40s -> monitor(40s) stale vs 30s threshold
    dto = build_health(store, _FakeAdapter(), now_ms_fn=lambda: 1_040_000,
                       stale_threshold_sec=30.0)

    by = {m.machine: m for m in dto.machines}
    assert by['monitor'].age_sec == 40.0
    assert by['monitor'].stale is True
    assert dto.equity == 499.0
    assert dto.cash == 400.0
    assert dto.balance_error is None
    assert dto.db_ok is True


def test_health_degrades_on_balance_error(store):
    HeartbeatRepository(store).beat('monitor', ts=1_000_000)
    dto = build_health(store, _FakeAdapter(raise_balance=True),
                       now_ms_fn=lambda: 1_005_000, stale_threshold_sec=30.0)
    assert dto.equity is None
    assert dto.balance_error is not None
    assert dto.db_ok is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_queries_health.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/dashboard/__init__.py
```
（空文件）

```python
# tests/dashboard/__init__.py
```
（空文件）

```python
# gridtrade/dashboard/queries.py
"""只读查询层：复用现有仓储 + 直读表做 dashboard 聚合，绝不写库/写交易所。"""
from dataclasses import dataclass
from typing import List, Optional

from gridtrade.runtime.introspect import adapter_endpoint
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.state.models import now_ms


@dataclass
class MachineHealth:
    machine: str
    last_beat_ts: int
    age_sec: float
    stale: bool


@dataclass
class HealthDTO:
    machines: List[MachineHealth]
    endpoint: str
    equity: Optional[float]
    cash: Optional[float]
    balance_error: Optional[str]
    db_ok: bool


def build_health(store, adapter, *, now_ms_fn=now_ms,
                 stale_threshold_sec: float = 30.0) -> HealthDTO:
    db_ok = True
    machines: List[MachineHealth] = []
    try:
        beats = HeartbeatRepository(store).list_all()
        now = now_ms_fn()
        for hb in sorted(beats, key=lambda b: b.machine):
            age = (now - hb.last_beat_ts) / 1000.0
            machines.append(MachineHealth(
                machine=hb.machine, last_beat_ts=hb.last_beat_ts,
                age_sec=age, stale=age > stale_threshold_sec))
    except Exception:
        db_ok = False

    equity = cash = None
    balance_error = None
    try:
        bal = adapter.fetch_balance()
        equity, cash = bal.equity, bal.cash
    except Exception as exc:
        balance_error = repr(exc)

    try:
        endpoint = adapter_endpoint(adapter)
    except Exception:
        endpoint = 'n/a'

    return HealthDTO(machines=machines, endpoint=endpoint, equity=equity,
                     cash=cash, balance_error=balance_error, db_ok=db_ok)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_queries_health.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/__init__.py gridtrade/dashboard/queries.py tests/dashboard/__init__.py tests/dashboard/test_queries_health.py
git commit -m "feat(dashboard): 只读查询层 build_health（心跳/余额/endpoint/DB）"
```

---

### Task 2: 只读查询层 — 活跃网格总览（GridOverviewRow）

**Files:**
- Modify: `gridtrade/dashboard/queries.py`
- Test: `tests/dashboard/test_queries_overview.py`

**Interfaces:**
- Consumes: `gridtrade.state.grids.GridRepository(store).list_active() -> List[Grid]`；`gridtrade.state.accounting.AccountingRepository(store).get(grid_id) -> Optional[Accounting]`；`gridtrade.state.orders.OrderRepository(store).list_open_by_grid(grid_id) -> List[GridOrder]`；adapter `fetch_price(symbol) -> float`。`Grid` 字段见 `gridtrade/state/models.py`（含 `id, symbol, status, direction, low_price, high_price, stop_low_price, stop_high_price`）；`Accounting` 含 `realized_pnl, net_position, avg_price, fee_paid, funding_paid`。
- Produces:
  - `@dataclass GridOverviewRow(grid_id, symbol, status, direction, low_price, high_price, open_order_count, net_position, avg_price, realized_pnl, current_price, unrealized_pnl, price_error, stop_low_price, stop_high_price, stop_low_dist_pct, stop_high_dist_pct)` —— 价相关字段 `Optional[float]`。
  - `def build_overview(store, adapter) -> List[GridOverviewRow]`
  - 内部 helper `def _unrealized(net_position, avg_price, price) -> float: return net_position * (price - avg_price)`

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_queries_overview.py
from gridtrade.dashboard.queries import build_overview
from gridtrade.state.grids import GridRepository
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.orders import OrderRepository
from gridtrade.state.models import Grid, GridOrder, ACTIVE


class _PriceAdapter:
    def __init__(self, prices, raise_for=()):
        self._p = prices
        self._raise_for = set(raise_for)

    def fetch_price(self, symbol):
        if symbol in self._raise_for:
            raise RuntimeError("ticker timeout")
        return self._p[symbol]


def test_overview_computes_unrealized_and_stop_distance(store):
    grids = GridRepository(store)
    accs = AccountingRepository(store)
    orders = OrderRepository(store)

    g = grids.create(Grid(id='g1', exchange='hyperliquid', symbol='BTC/USDT:USDT',
                          status=ACTIVE, direction='neutral',
                          low_price=90.0, high_price=110.0,
                          stop_low_price=80.0, stop_high_price=120.0))
    accs.init('g1')
    acc = accs.get('g1')
    acc.net_position = 2.0
    acc.avg_price = 100.0
    acc.realized_pnl = 5.0
    accs.save(acc)
    orders.upsert(GridOrder(client_oid='o1', grid_id='g1', line_index=0,
                            side='buy', price=95.0, size=1.0, status='open'))

    rows = build_overview(store, _PriceAdapter({'BTC/USDT:USDT': 105.0}))
    assert len(rows) == 1
    r = rows[0]
    assert r.grid_id == 'g1'
    assert r.open_order_count == 1
    assert r.current_price == 105.0
    assert r.unrealized_pnl == 10.0          # 2 * (105 - 100)
    assert r.realized_pnl == 5.0
    assert r.price_error is None
    # 现价 105：距上止损 120 -> (120-105)/105 ; 距下止损 80 -> (105-80)/105
    assert round(r.stop_high_dist_pct, 4) == round((120.0 - 105.0) / 105.0, 4)
    assert round(r.stop_low_dist_pct, 4) == round((105.0 - 80.0) / 105.0, 4)


def test_overview_degrades_when_price_unavailable(store):
    grids = GridRepository(store)
    grids.create(Grid(id='g1', exchange='hyperliquid', symbol='ETH/USDT:USDT',
                      status=ACTIVE, direction='neutral'))
    AccountingRepository(store).init('g1')
    rows = build_overview(store, _PriceAdapter({}, raise_for={'ETH/USDT:USDT'}))
    r = rows[0]
    assert r.current_price is None
    assert r.unrealized_pnl is None
    assert r.price_error is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_queries_overview.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_overview'`

- [ ] **Step 3: Write minimal implementation** (append to `gridtrade/dashboard/queries.py`)

```python
# --- 追加到 gridtrade/dashboard/queries.py ---
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository


@dataclass
class GridOverviewRow:
    grid_id: str
    symbol: str
    status: str
    direction: str
    low_price: Optional[float]
    high_price: Optional[float]
    open_order_count: int
    net_position: float
    avg_price: float
    realized_pnl: float
    current_price: Optional[float]
    unrealized_pnl: Optional[float]
    price_error: Optional[str]
    stop_low_price: Optional[float]
    stop_high_price: Optional[float]
    stop_low_dist_pct: Optional[float]
    stop_high_dist_pct: Optional[float]


def _unrealized(net_position: float, avg_price: float, price: float) -> float:
    return net_position * (price - avg_price)


def build_overview(store, adapter) -> List[GridOverviewRow]:
    grids = GridRepository(store)
    accs = AccountingRepository(store)
    orders = OrderRepository(store)
    rows: List[GridOverviewRow] = []
    for g in sorted(grids.list_active(), key=lambda x: x.symbol):
        acc = accs.get(g.id)
        net = acc.net_position if acc else 0.0
        avg = acc.avg_price if acc else 0.0
        realized = acc.realized_pnl if acc else 0.0
        open_n = len(orders.list_open_by_grid(g.id))

        price = unreal = None
        price_error = None
        low_dist = high_dist = None
        try:
            price = adapter.fetch_price(g.symbol)
            unreal = _unrealized(net, avg, price)
            if g.stop_low_price is not None and price:
                low_dist = (price - g.stop_low_price) / price
            if g.stop_high_price is not None and price:
                high_dist = (g.stop_high_price - price) / price
        except Exception as exc:
            price_error = repr(exc)

        rows.append(GridOverviewRow(
            grid_id=g.id, symbol=g.symbol, status=g.status, direction=g.direction,
            low_price=g.low_price, high_price=g.high_price, open_order_count=open_n,
            net_position=net, avg_price=avg, realized_pnl=realized,
            current_price=price, unrealized_pnl=unreal, price_error=price_error,
            stop_low_price=g.stop_low_price, stop_high_price=g.stop_high_price,
            stop_low_dist_pct=low_dist, stop_high_dist_pct=high_dist))
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_queries_overview.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/queries.py tests/dashboard/test_queries_overview.py
git commit -m "feat(dashboard): build_overview（活跃网格 + 未实现盈亏 + 止损距离 + 优雅退化）"
```

---

### Task 3: 只读查询层 — 单网格明细（GridDetailDTO）

**Files:**
- Modify: `gridtrade/dashboard/queries.py`
- Test: `tests/dashboard/test_queries_detail.py`

**Interfaces:**
- Consumes: `GridRepository.get(grid_id) -> Optional[Grid]`；`OrderRepository.list_by_grid(grid_id) -> List[GridOrder]`；`gridtrade.state.fills.FillRepository(store).list_by_grid(grid_id) -> List[Fill]`；`AccountingRepository.get(grid_id) -> Optional[Accounting]`。`GridOrder` 含 `line_index, side, price, size, status`；`Fill` 含 `line_index, side, price, size, ts`。
- Produces:
  - `@dataclass GridDetailDTO(grid: Grid, orders: List[GridOrder], fills: List[Fill], accounting: Optional[Accounting])`
  - `def build_grid_detail(store, grid_id: str, *, fills_limit: int = 50) -> Optional[GridDetailDTO]` —— grid 不存在返回 `None`；`orders` 按 `line_index` 升序；`fills` 按 `ts` 降序取前 `fills_limit` 条。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_queries_detail.py
from gridtrade.dashboard.queries import build_grid_detail
from gridtrade.state.grids import GridRepository
from gridtrade.state.orders import OrderRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.models import Grid, GridOrder, Fill, ACTIVE


def test_detail_returns_orders_sorted_and_fills_recent_first(store):
    GridRepository(store).create(Grid(id='g1', exchange='hyperliquid',
                                      symbol='BTC/USDT:USDT', status=ACTIVE))
    orders = OrderRepository(store)
    orders.upsert(GridOrder(client_oid='o2', grid_id='g1', line_index=2,
                            side='sell', price=110.0, size=1.0, status='open'))
    orders.upsert(GridOrder(client_oid='o1', grid_id='g1', line_index=1,
                            side='buy', price=90.0, size=1.0, status='open'))
    fills = FillRepository(store)
    fills.add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=1, side='buy',
                          price=90.0, size=1.0, ts=1000))
    fills.add_if_new(Fill(trade_id='t2', grid_id='g1', line_index=2, side='sell',
                          price=110.0, size=1.0, ts=2000))
    AccountingRepository(store).init('g1')

    dto = build_grid_detail(store, 'g1')
    assert dto is not None
    assert [o.line_index for o in dto.orders] == [1, 2]
    assert [f.trade_id for f in dto.fills] == ['t2', 't1']   # ts 降序
    assert dto.accounting is not None


def test_detail_returns_none_for_missing_grid(store):
    assert build_grid_detail(store, 'nope') is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_queries_detail.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_grid_detail'`

- [ ] **Step 3: Write minimal implementation** (append to `gridtrade/dashboard/queries.py`)

```python
# --- 追加到 gridtrade/dashboard/queries.py ---
from gridtrade.state.fills import FillRepository


@dataclass
class GridDetailDTO:
    grid: object
    orders: list
    fills: list
    accounting: object


def build_grid_detail(store, grid_id: str, *, fills_limit: int = 50):
    grid = GridRepository(store).get(grid_id)
    if grid is None:
        return None
    orders = sorted(OrderRepository(store).list_by_grid(grid_id),
                    key=lambda o: o.line_index)
    fills = sorted(FillRepository(store).list_by_grid(grid_id),
                   key=lambda f: f.ts, reverse=True)[:fills_limit]
    acc = AccountingRepository(store).get(grid_id)
    return GridDetailDTO(grid=grid, orders=orders, fills=fills, accounting=acc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_queries_detail.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/queries.py tests/dashboard/test_queries_detail.py
git commit -m "feat(dashboard): build_grid_detail（挂单/成交流水/记账明细）"
```

---

### Task 4: 只读查询层 — 历史战绩 + tag 聚合 + 全局成交流（RecordsDTO）

**Files:**
- Modify: `gridtrade/dashboard/queries.py`
- Test: `tests/dashboard/test_queries_records.py`

**Interfaces:**
- Consumes: 直读表 `gridtrade.state.models.order_records` 与 `grid_fills`（用 `store.engine` + SQLAlchemy Core `select`），因现有仓储只有 `list_by_tag`/`list_by_grid`，没有「全量」读法。`order_records` 列见 models：`symbol, tag, total_pnl, pnl_ratio, exit_reason, closed_at, created_at`；只统计 `closed_at IS NOT NULL` 的已平记录。
- Produces:
  - `@dataclass TagSummary(tag: str, count: int, total_pnl: float, win_count: int, win_rate: float)`
  - `@dataclass RecordRow(id, symbol, tag, total_pnl, pnl_ratio, exit_reason, closed_at)`
  - `@dataclass RecentFill(grid_id, line_index, side, price, size, ts)`
  - `@dataclass RecordsDTO(records: List[RecordRow], tag_summaries: List[TagSummary], recent_fills: List[RecentFill])`
  - `def build_records(store, *, records_limit: int = 200, fills_limit: int = 50) -> RecordsDTO` —— records 按 `closed_at` 降序；胜=`total_pnl > 0`；`win_rate = win_count / count`（count 为 0 时 0.0）。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_queries_records.py
from gridtrade.dashboard.queries import build_records
from gridtrade.state.records import RecordRepository
from gridtrade.state.fills import FillRepository
from gridtrade.state.models import Record, Fill


def test_records_aggregates_by_tag_and_orders_recent_first(store):
    recs = RecordRepository(store)
    recs.add(Record(id='r1', exchange='hyperliquid', symbol='BTC/USDT:USDT',
                    tag='gt0', total_pnl=10.0, pnl_ratio=0.1,
                    exit_reason='take_profit', closed_at=2000))
    recs.add(Record(id='r2', exchange='hyperliquid', symbol='ETH/USDT:USDT',
                    tag='gt0', total_pnl=-4.0, pnl_ratio=-0.04,
                    exit_reason='stop_loss', closed_at=3000))
    recs.add(Record(id='r3', exchange='hyperliquid', symbol='SOL/USDT:USDT',
                    tag='gt1', total_pnl=2.0, pnl_ratio=0.02,
                    exit_reason='take_profit', closed_at=1000))
    FillRepository(store).add_if_new(Fill(trade_id='t1', grid_id='g1', line_index=0,
                                          side='buy', price=90.0, size=1.0, ts=5000))

    dto = build_records(store)
    assert [r.id for r in dto.records] == ['r2', 'r1', 'r3']   # closed_at 降序
    by = {s.tag: s for s in dto.tag_summaries}
    assert by['gt0'].count == 2
    assert by['gt0'].total_pnl == 6.0       # 10 - 4
    assert by['gt0'].win_count == 1
    assert round(by['gt0'].win_rate, 4) == 0.5
    assert by['gt1'].count == 1
    assert len(dto.recent_fills) == 1
    assert dto.recent_fills[0].grid_id == 'g1'


def test_records_ignores_unclosed_records(store):
    RecordRepository(store).add(Record(id='open1', exchange='hyperliquid',
                                       symbol='BTC/USDT:USDT', tag='gt0',
                                       closed_at=None))
    dto = build_records(store)
    assert dto.records == []
    assert dto.tag_summaries == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_queries_records.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_records'`

- [ ] **Step 3: Write minimal implementation** (append to `gridtrade/dashboard/queries.py`)

```python
# --- 追加到 gridtrade/dashboard/queries.py ---
from sqlalchemy import select
from gridtrade.state.models import order_records, grid_fills


@dataclass
class TagSummary:
    tag: str
    count: int
    total_pnl: float
    win_count: int
    win_rate: float


@dataclass
class RecordRow:
    id: str
    symbol: str
    tag: str
    total_pnl: Optional[float]
    pnl_ratio: Optional[float]
    exit_reason: Optional[str]
    closed_at: Optional[int]


@dataclass
class RecentFill:
    grid_id: str
    line_index: int
    side: str
    price: float
    size: float
    ts: int


@dataclass
class RecordsDTO:
    records: List[RecordRow]
    tag_summaries: List[TagSummary]
    recent_fills: List[RecentFill]


def build_records(store, *, records_limit: int = 200,
                  fills_limit: int = 50) -> RecordsDTO:
    with store.engine.connect() as c:
        rows = c.execute(
            select(order_records)
            .where(order_records.c.closed_at.isnot(None))
            .order_by(order_records.c.closed_at.desc())
            .limit(records_limit)
        ).all()
        fill_rows = c.execute(
            select(grid_fills).order_by(grid_fills.c.ts.desc()).limit(fills_limit)
        ).all()

    records = [RecordRow(id=r._mapping['id'], symbol=r._mapping['symbol'],
                         tag=r._mapping['tag'], total_pnl=r._mapping['total_pnl'],
                         pnl_ratio=r._mapping['pnl_ratio'],
                         exit_reason=r._mapping['exit_reason'],
                         closed_at=r._mapping['closed_at']) for r in rows]

    agg = {}
    for r in records:
        s = agg.setdefault(r.tag, {'count': 0, 'total': 0.0, 'win': 0})
        s['count'] += 1
        s['total'] += (r.total_pnl or 0.0)
        if (r.total_pnl or 0.0) > 0:
            s['win'] += 1
    tag_summaries = [
        TagSummary(tag=t, count=v['count'], total_pnl=v['total'],
                   win_count=v['win'],
                   win_rate=(v['win'] / v['count'] if v['count'] else 0.0))
        for t, v in sorted(agg.items())]

    recent_fills = [RecentFill(grid_id=f._mapping['grid_id'],
                               line_index=f._mapping['line_index'],
                               side=f._mapping['side'], price=f._mapping['price'],
                               size=f._mapping['size'], ts=f._mapping['ts'])
                    for f in fill_rows]
    return RecordsDTO(records=records, tag_summaries=tag_summaries,
                      recent_fills=recent_fills)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_queries_records.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/queries.py tests/dashboard/test_queries_records.py
git commit -m "feat(dashboard): build_records（历史战绩 + tag 聚合 + 全局成交流）"
```

---

### Task 5: 展示格式化工具（Jinja filter）

**Files:**
- Create: `gridtrade/dashboard/formatting.py`
- Test: `tests/dashboard/test_formatting.py`

**Interfaces:**
- Produces:
  - `def ms_to_human(ts: Optional[int]) -> str` —— None → `'-'`；否则 UTC `'YYYY-MM-DD HH:MM:SS'`。
  - `def age_human(sec: Optional[float]) -> str` —— None → `'-'`；`<60` → `'{n}s'`；`<3600` → `'{m}m'`；否则 `'{h}h'`（整数截断）。
  - `def fmt_num(x: Optional[float], digits: int = 2) -> str` —— None → `'-'`；否则定点小数。
  - `def fmt_pct(x: Optional[float], digits: int = 2) -> str` —— None → `'-'`；否则 `x*100` 加 `'%'`。
  - `def pnl_class(x: Optional[float]) -> str` —— `>0` → `'pos'`；`<0` → `'neg'`；其余 → `'zero'`（用于 CSS 着色）。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_formatting.py
from gridtrade.dashboard.formatting import (ms_to_human, age_human, fmt_num,
                                            fmt_pct, pnl_class)


def test_ms_to_human():
    assert ms_to_human(None) == '-'
    assert ms_to_human(0) == '1970-01-01 00:00:00'


def test_age_human():
    assert age_human(None) == '-'
    assert age_human(5) == '5s'
    assert age_human(90) == '1m'
    assert age_human(7200) == '2h'


def test_fmt_num_and_pct():
    assert fmt_num(None) == '-'
    assert fmt_num(1.2345, 2) == '1.23'
    assert fmt_pct(None) == '-'
    assert fmt_pct(0.1234, 1) == '12.3%'


def test_pnl_class():
    assert pnl_class(3.0) == 'pos'
    assert pnl_class(-3.0) == 'neg'
    assert pnl_class(0.0) == 'zero'
    assert pnl_class(None) == 'zero'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_formatting.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard.formatting`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/dashboard/formatting.py
"""Jinja2 展示格式化：时间/数字/百分比/盈亏着色。纯函数，无副作用。"""
from datetime import datetime, timezone
from typing import Optional


def ms_to_human(ts: Optional[int]) -> str:
    if ts is None:
        return '-'
    return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime(
        '%Y-%m-%d %H:%M:%S')


def age_human(sec: Optional[float]) -> str:
    if sec is None:
        return '-'
    sec = int(sec)
    if sec < 60:
        return '%ds' % sec
    if sec < 3600:
        return '%dm' % (sec // 60)
    return '%dh' % (sec // 3600)


def fmt_num(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return '-'
    return f'{x:.{digits}f}'


def fmt_pct(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return '-'
    return f'{x * 100:.{digits}f}%'


def pnl_class(x: Optional[float]) -> str:
    if x is None or x == 0:
        return 'zero'
    return 'pos' if x > 0 else 'neg'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_formatting.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/formatting.py tests/dashboard/test_formatting.py
git commit -m "feat(dashboard): 展示格式化 Jinja filter（时间/数字/百分比/盈亏着色）"
```

---

### Task 6: 登录鉴权 — 密码哈希 + 会话签名 + 失败锁定（auth.py）

**Files:**
- Create: `gridtrade/dashboard/auth.py`
- Test: `tests/dashboard/test_auth.py`

**Interfaces:**
- Produces（仅标准库 `hashlib`/`hmac`/`secrets`/`base64`/`time`）：
  - `def hash_password(password: str, *, iterations: int = 200_000) -> str` —— 返回 `'pbkdf2$<iter>$<salt_hex>$<hash_hex>'`。
  - `def verify_password(password: str, encoded: str) -> bool` —— 恒定时间比较；格式非法/不匹配返回 `False`。
  - `def make_session(username: str, secret: str, *, ttl_sec: int = 86400, now_fn=time.time) -> str` —— 返回签名 token `'<payload_b64>.<sig_hex>'`，payload 含 username 与过期时间。
  - `def verify_session(token: str, secret: str, *, now_fn=time.time) -> Optional[str]` —— 验签 + 未过期则返回 username，否则 `None`。
  - `class LoginThrottle(max_attempts=5, lockout_sec=3600, now_fn=time.time)`：
    - `.is_locked(key: str) -> bool`
    - `.record_failure(key: str) -> None`
    - `.record_success(key: str) -> None`（清零该 key）

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_auth.py
from gridtrade.dashboard.auth import (hash_password, verify_password,
                                      make_session, verify_session, LoginThrottle)


def test_password_hash_roundtrip():
    enc = hash_password('s3cret', iterations=1000)
    assert verify_password('s3cret', enc) is True
    assert verify_password('wrong', enc) is False
    assert verify_password('s3cret', 'garbage') is False


def test_session_roundtrip_and_expiry():
    tok = make_session('admin', 'topsecret', ttl_sec=100, now_fn=lambda: 1000)
    assert verify_session(tok, 'topsecret', now_fn=lambda: 1050) == 'admin'
    assert verify_session(tok, 'topsecret', now_fn=lambda: 2000) is None     # 过期
    assert verify_session(tok, 'wrongsecret', now_fn=lambda: 1050) is None   # 验签失败
    assert verify_session('not.a.token', 'topsecret') is None


def test_throttle_locks_after_max_attempts():
    t = [1000.0]
    thr = LoginThrottle(max_attempts=3, lockout_sec=3600, now_fn=lambda: t[0])
    for _ in range(3):
        assert thr.is_locked('admin') is False
        thr.record_failure('admin')
    assert thr.is_locked('admin') is True            # 达上限 -> 锁定
    t[0] += 3599
    assert thr.is_locked('admin') is True            # 1h 内仍锁
    t[0] += 2
    assert thr.is_locked('admin') is False           # 超 1h 解锁


def test_throttle_success_resets():
    thr = LoginThrottle(max_attempts=3, lockout_sec=3600, now_fn=lambda: 1000.0)
    thr.record_failure('admin')
    thr.record_failure('admin')
    thr.record_success('admin')
    thr.record_failure('admin')
    assert thr.is_locked('admin') is False           # 计数已被成功清零
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard.auth`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/dashboard/auth.py
"""登录鉴权：pbkdf2 密码哈希 + HMAC 签名会话 + 失败计数锁定。仅标准库。"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional


def hash_password(password: str, *, iterations: int = 200_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    return 'pbkdf2$%d$%s$%s' % (iterations, salt.hex(), dk.hex())


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iters, salt_hex, hash_hex = encoded.split('$')
        if scheme != 'pbkdf2':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def make_session(username: str, secret: str, *, ttl_sec: int = 86400,
                 now_fn=time.time) -> str:
    payload = json.dumps({'u': username, 'exp': int(now_fn()) + ttl_sec}).encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return '%s.%s' % (_b64(payload), sig)


def verify_session(token: str, secret: str, *, now_fn=time.time) -> Optional[str]:
    try:
        payload_b64, sig = token.split('.')
        payload = _unb64(payload_b64)
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        data = json.loads(payload)
        if int(now_fn()) >= int(data['exp']):
            return None
        return data['u']
    except Exception:
        return None


class LoginThrottle:
    """按 key（用户名/IP）记失败次数；达 max_attempts 锁定 lockout_sec。内存态。"""

    def __init__(self, max_attempts: int = 5, lockout_sec: int = 3600,
                 now_fn=time.time):
        self.max_attempts = max_attempts
        self.lockout_sec = lockout_sec
        self._now = now_fn
        self._fails = {}          # key -> count
        self._locked_until = {}   # key -> ts

    def is_locked(self, key: str) -> bool:
        until = self._locked_until.get(key)
        if until is None:
            return False
        if self._now() >= until:
            self._locked_until.pop(key, None)
            self._fails.pop(key, None)
            return False
        return True

    def record_failure(self, key: str) -> None:
        self._fails[key] = self._fails.get(key, 0) + 1
        if self._fails[key] >= self.max_attempts:
            self._locked_until[key] = self._now() + self.lockout_sec

    def record_success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked_until.pop(key, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_auth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/auth.py tests/dashboard/test_auth.py
git commit -m "feat(dashboard): 登录鉴权（pbkdf2 + 签名会话 + 失败锁定）"
```

---

### Task 7: FastAPI 应用工厂 + 路由 + 模板（app.py）

**Files:**
- Modify: `requirements.txt`
- Create: `gridtrade/dashboard/app.py`
- Create: `gridtrade/dashboard/templates/base.html`
- Create: `gridtrade/dashboard/templates/login.html`
- Create: `gridtrade/dashboard/templates/overview.html`
- Create: `gridtrade/dashboard/templates/detail.html`
- Create: `gridtrade/dashboard/templates/history.html`
- Create: `gridtrade/dashboard/static/htmx.min.js`（vendored 占位，可后续替换为真文件）
- Create: `gridtrade/dashboard/static/app.css`
- Test: `tests/dashboard/test_app.py`

**Interfaces:**
- Consumes: `build_health`, `build_overview`, `build_grid_detail`, `build_records`（queries.py）；`verify_password`, `make_session`, `verify_session`, `LoginThrottle`（auth.py）；格式化函数（formatting.py）。
- Produces:
  - `def create_app(store, adapter, *, username: str, password_hash: str, session_secret: str, throttle: Optional[LoginThrottle] = None, stale_threshold_sec: float = 30.0) -> FastAPI`
  - 路由：`GET /login`、`POST /login`、`GET /`（overview）、`GET /grid/{grid_id}`、`GET /history`、`GET /static/...`；会话 cookie 名 `gt_session`。
  - 未登录访问受保护路由 → 302 跳 `/login`。

- [ ] **Step 1: Add dependencies**

在 `requirements.txt` 末尾追加：

```
# Dashboard（web 进程；monitor/scheduler 不依赖）
fastapi>=0.110,<0.116
uvicorn[standard]>=0.27,<0.35
jinja2>=3.1,<4
```

安装：`.venv/bin/pip install "fastapi>=0.110,<0.116" "uvicorn[standard]>=0.27,<0.35" "jinja2>=3.1,<4"`

- [ ] **Step 2: Write the failing test**

```python
# tests/dashboard/test_app.py
import pytest
from starlette.testclient import TestClient

from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password, LoginThrottle
from gridtrade.state.grids import GridRepository
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.models import Grid, ACTIVE
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None

    def fetch_balance(self):
        return Balance(equity=499.0, cash=400.0)

    def fetch_price(self, symbol):
        return 100.0


def _client(store, throttle=None):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000),
                     session_secret='sekret', throttle=throttle)
    return TestClient(app)


def test_unauthenticated_redirects_to_login(store):
    c = _client(store)
    r = c.get('/', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['location'].endswith('/login')


def test_login_then_overview_shows_grid(store):
    GridRepository(store).create(Grid(id='g1', exchange='hyperliquid',
                                      symbol='BTC/USDT:USDT', status=ACTIVE))
    AccountingRepository(store).init('g1')
    c = _client(store)
    r = c.post('/login', data={'username': 'admin', 'password': 'pw'},
               follow_redirects=False)
    assert r.status_code == 302
    home = c.get('/')
    assert home.status_code == 200
    assert 'BTC/USDT:USDT' in home.text


def test_wrong_password_then_lockout(store):
    thr = LoginThrottle(max_attempts=3, lockout_sec=3600, now_fn=lambda: 1000.0)
    c = _client(store, throttle=thr)
    for _ in range(3):
        bad = c.post('/login', data={'username': 'admin', 'password': 'nope'})
        assert 'error' in bad.text.lower() or bad.status_code == 401
    # 锁定后，即使密码正确也拒
    locked = c.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert 'locked' in locked.text.lower() or locked.status_code == 429
```

- [ ] **Step 3: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard.app`

- [ ] **Step 4: Write minimal implementation**

```python
# gridtrade/dashboard/app.py
"""FastAPI 应用工厂：登录鉴权 + 四个只读视图。web 进程绝不写库/写交易所。"""
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gridtrade.dashboard import formatting as fmt
from gridtrade.dashboard.auth import (LoginThrottle, make_session,
                                      verify_password, verify_session)
from gridtrade.dashboard.queries import (build_grid_detail, build_health,
                                         build_overview, build_records)

_DIR = Path(__file__).parent
_COOKIE = 'gt_session'


def create_app(store, adapter, *, username: str, password_hash: str,
               session_secret: str, throttle: Optional[LoginThrottle] = None,
               stale_threshold_sec: float = 30.0) -> FastAPI:
    app = FastAPI()
    throttle = throttle or LoginThrottle()
    templates = Jinja2Templates(directory=str(_DIR / 'templates'))
    for name, func in (('ms_to_human', fmt.ms_to_human), ('age_human', fmt.age_human),
                       ('fmt_num', fmt.fmt_num), ('fmt_pct', fmt.fmt_pct),
                       ('pnl_class', fmt.pnl_class)):
        templates.env.filters[name] = func
    app.mount('/static', StaticFiles(directory=str(_DIR / 'static')), name='static')

    def _user(request: Request) -> Optional[str]:
        tok = request.cookies.get(_COOKIE)
        return verify_session(tok, session_secret) if tok else None

    def _health():
        return build_health(store, adapter, stale_threshold_sec=stale_threshold_sec)

    @app.get('/login', response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse('login.html',
                                          {'request': request, 'error': None})

    @app.post('/login')
    def login(request: Request, username_in: str = Form(alias='username'),
              password: str = Form(...)):
        if throttle.is_locked(username_in):
            return templates.TemplateResponse(
                'login.html', {'request': request, 'error': 'account locked'},
                status_code=429)
        if username_in == username and verify_password(password, password_hash):
            throttle.record_success(username_in)
            resp = RedirectResponse('/', status_code=302)
            resp.set_cookie(_COOKIE, make_session(username_in, session_secret),
                            httponly=True, samesite='lax', secure=True)
            return resp
        throttle.record_failure(username_in)
        return templates.TemplateResponse(
            'login.html', {'request': request, 'error': 'invalid credentials'},
            status_code=401)

    @app.get('/', response_class=HTMLResponse)
    def overview(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        return templates.TemplateResponse('overview.html', {
            'request': request, 'health': _health(),
            'rows': build_overview(store, adapter)})

    @app.get('/grid/{grid_id}', response_class=HTMLResponse)
    def detail(request: Request, grid_id: str):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        dto = build_grid_detail(store, grid_id)
        if dto is None:
            return HTMLResponse('grid not found', status_code=404)
        return templates.TemplateResponse('detail.html', {
            'request': request, 'health': _health(), 'd': dto})

    @app.get('/history', response_class=HTMLResponse)
    def history(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        return templates.TemplateResponse('history.html', {
            'request': request, 'health': _health(),
            'r': build_records(store)})

    return app
```

模板 `base.html`（健康顶栏 + 内容块）：

```html
<!-- gridtrade/dashboard/templates/base.html -->
<!doctype html>
<html>
<head>
  <meta charset="utf-8"><title>GridTradeGP</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/htmx.min.js"></script>
</head>
<body>
{% if health is defined %}
<header class="health">
  <span>endpoint: {{ health.endpoint }}</span>
  <span>equity: {{ health.equity | fmt_num }}</span>
  <span>cash: {{ health.cash | fmt_num }}</span>
  <span>DB: {{ 'ok' if health.db_ok else 'DOWN' }}</span>
  {% for m in health.machines %}
    <span class="{{ 'neg' if m.stale else 'pos' }}">
      {{ m.machine }}: {{ m.age_sec | age_human }}{{ ' STALE' if m.stale }}</span>
  {% endfor %}
  {% if health.balance_error %}<span class="neg">balance err</span>{% endif %}
  <nav><a href="/">overview</a> <a href="/history">history</a></nav>
</header>
{% endif %}
<main>{% block content %}{% endblock %}</main>
</body>
</html>
```

`login.html`：

```html
<!-- gridtrade/dashboard/templates/login.html -->
<!doctype html><html><head><meta charset="utf-8"><title>login</title>
<link rel="stylesheet" href="/static/app.css"></head><body>
<form method="post" action="/login" class="login">
  <h2>GridTradeGP</h2>
  {% if error %}<p class="neg">{{ error }}</p>{% endif %}
  <input name="username" placeholder="username" autofocus>
  <input name="password" type="password" placeholder="password">
  <button type="submit">login</button>
</form></body></html>
```

`overview.html`（5s 轮询整页刷新；HTMX 局部刷新留到后续打磨）：

```html
<!-- gridtrade/dashboard/templates/overview.html -->
{% extends "base.html" %}{% block content %}
<meta http-equiv="refresh" content="8">
<h1>Active Grids</h1>
<table><thead><tr>
<th>symbol</th><th>status</th><th>dir</th><th>range</th><th>orders</th>
<th>net</th><th>realized</th><th>unreal</th><th>price</th><th>stop dist</th></tr></thead>
<tbody>
{% for r in rows %}
<tr>
  <td><a href="/grid/{{ r.grid_id }}">{{ r.symbol }}</a></td>
  <td>{{ r.status }}</td><td>{{ r.direction }}</td>
  <td>{{ r.low_price | fmt_num }}–{{ r.high_price | fmt_num }}</td>
  <td>{{ r.open_order_count }}</td>
  <td>{{ r.net_position | fmt_num }}</td>
  <td class="{{ r.realized_pnl | pnl_class }}">{{ r.realized_pnl | fmt_num }}</td>
  <td class="{{ r.unrealized_pnl | pnl_class }}">{{ r.unrealized_pnl | fmt_num }}</td>
  <td>{{ r.current_price | fmt_num }}{% if r.price_error %} <span class="neg">(stale)</span>{% endif %}</td>
  <td>L {{ r.stop_low_dist_pct | fmt_pct }} / H {{ r.stop_high_dist_pct | fmt_pct }}</td>
</tr>
{% endfor %}
</tbody></table>
{% if not rows %}<p>no active grids</p>{% endif %}
{% endblock %}
```

`detail.html`：

```html
<!-- gridtrade/dashboard/templates/detail.html -->
{% extends "base.html" %}{% block content %}
<h1>{{ d.grid.symbol }} <small>{{ d.grid.id }}</small></h1>
<p>status={{ d.grid.status }} dir={{ d.grid.direction }}
   range={{ d.grid.low_price | fmt_num }}–{{ d.grid.high_price | fmt_num }}
   stop={{ d.grid.stop_low_price | fmt_num }}/{{ d.grid.stop_high_price | fmt_num }}</p>
{% if d.accounting %}
<p>realized={{ d.accounting.realized_pnl | fmt_num }}
   fee={{ d.accounting.fee_paid | fmt_num }}
   funding={{ d.accounting.funding_paid | fmt_num }}
   net={{ d.accounting.net_position | fmt_num }}
   avg={{ d.accounting.avg_price | fmt_num }}
   peak={{ d.accounting.pnl_ratio_max | fmt_pct }}</p>
{% endif %}
<h2>Orders</h2>
<table><thead><tr><th>line</th><th>side</th><th>price</th><th>size</th><th>status</th></tr></thead><tbody>
{% for o in d.orders %}<tr><td>{{ o.line_index }}</td><td>{{ o.side }}</td>
<td>{{ o.price | fmt_num }}</td><td>{{ o.size | fmt_num }}</td><td>{{ o.status }}</td></tr>{% endfor %}
</tbody></table>
<h2>Recent Fills</h2>
<table><thead><tr><th>ts</th><th>line</th><th>side</th><th>price</th><th>size</th></tr></thead><tbody>
{% for f in d.fills %}<tr><td>{{ f.ts | ms_to_human }}</td><td>{{ f.line_index }}</td>
<td>{{ f.side }}</td><td>{{ f.price | fmt_num }}</td><td>{{ f.size | fmt_num }}</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

`history.html`：

```html
<!-- gridtrade/dashboard/templates/history.html -->
{% extends "base.html" %}{% block content %}
<h1>By Tag</h1>
<table><thead><tr><th>tag</th><th>count</th><th>total pnl</th><th>win</th><th>win rate</th></tr></thead><tbody>
{% for s in r.tag_summaries %}<tr><td>{{ s.tag }}</td><td>{{ s.count }}</td>
<td class="{{ s.total_pnl | pnl_class }}">{{ s.total_pnl | fmt_num }}</td>
<td>{{ s.win_count }}</td><td>{{ s.win_rate | fmt_pct }}</td></tr>{% endfor %}
</tbody></table>
<h1>Closed Grids</h1>
<table><thead><tr><th>closed</th><th>symbol</th><th>tag</th><th>pnl</th><th>ratio</th><th>reason</th></tr></thead><tbody>
{% for rec in r.records %}<tr><td>{{ rec.closed_at | ms_to_human }}</td><td>{{ rec.symbol }}</td>
<td>{{ rec.tag }}</td><td class="{{ rec.total_pnl | pnl_class }}">{{ rec.total_pnl | fmt_num }}</td>
<td>{{ rec.pnl_ratio | fmt_pct }}</td><td>{{ rec.exit_reason }}</td></tr>{% endfor %}
</tbody></table>
<h1>Recent Fills (global)</h1>
<table><thead><tr><th>ts</th><th>grid</th><th>line</th><th>side</th><th>price</th><th>size</th></tr></thead><tbody>
{% for f in r.recent_fills %}<tr><td>{{ f.ts | ms_to_human }}</td><td>{{ f.grid_id }}</td>
<td>{{ f.line_index }}</td><td>{{ f.side }}</td><td>{{ f.price | fmt_num }}</td><td>{{ f.size | fmt_num }}</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

`app.css`（极简）：

```css
/* gridtrade/dashboard/static/app.css */
body{font-family:system-ui,monospace;margin:1rem;background:#111;color:#ddd}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
th,td{border:1px solid #333;padding:.3rem .5rem;text-align:right;font-size:.85rem}
th{background:#222}
.health{display:flex;gap:1rem;flex-wrap:wrap;padding:.5rem;background:#000;border-bottom:1px solid #333}
.health nav{margin-left:auto}
a{color:#6cf}
.pos{color:#4caf50}.neg{color:#e53935}.zero{color:#999}
.login{max-width:300px;margin:4rem auto;display:flex;flex-direction:column;gap:.5rem}
input,button{padding:.5rem;background:#222;color:#ddd;border:1px solid #444}
```

`htmx.min.js`（占位文件；本期模板用 meta refresh 轮询，HTMX 留作 P3 局部刷新升级）：

```javascript
/* gridtrade/dashboard/static/htmx.min.js — vendored placeholder; replace with real htmx for partial refresh in P3 */
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Run full dashboard suite**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/ -v`
Expected: PASS（全部 dashboard 测试绿）

- [ ] **Step 7: Commit**

```bash
git add requirements.txt gridtrade/dashboard/app.py gridtrade/dashboard/templates gridtrade/dashboard/static tests/dashboard/test_app.py
git commit -m "feat(dashboard): FastAPI 应用工厂 + 登录 + 四视图模板"
```

---

### Task 8: web 进程入口（runtime/web.py）

**Files:**
- Create: `gridtrade/runtime/web.py`
- Modify: `gridtrade/config.py`（`DeployConfig` 增 dashboard 字段）
- Test: `tests/dashboard/test_web_entrypoint.py`

**Interfaces:**
- Consumes: `gridtrade.config.load_deploy_config()`；`gridtrade.runtime.factory.build_runtime(config)`（复用其 `adapter`/`store`）；`create_app(...)`。
- Produces:
  - `gridtrade/config.py`：`DeployConfig` 追加 `dashboard_user: str = 'admin'`、`dashboard_password_hash: str = ''`、`dashboard_session_secret: str = ''`、`dashboard_port: int = 8080`；`load_deploy_config` 从 `DASHBOARD_USER` / `DASHBOARD_PASSWORD_HASH` / `DASHBOARD_SESSION_SECRET` / `PORT`(默认 8080) 读取。
  - `gridtrade/runtime/web.py`：`def build_web_app(config=None)` 组装并返回 FastAPI app（可测）；`def main()` composition root 起 uvicorn（不单测）。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_web_entrypoint.py
from gridtrade.config import load_deploy_config
from gridtrade.runtime.web import build_web_app
from gridtrade.dashboard.auth import hash_password


def test_build_web_app_offline():
    env = {
        'EXCHANGE': 'fake', 'DATABASE_URL': '',
        'DASHBOARD_USER': 'admin',
        'DASHBOARD_PASSWORD_HASH': hash_password('pw', iterations=1000),
        'DASHBOARD_SESSION_SECRET': 'sekret',
    }
    cfg = load_deploy_config(env)
    app = build_web_app(cfg)
    # FastAPI 应用，挂了我们的路由
    paths = {r.path for r in app.routes}
    assert '/login' in paths and '/' in paths and '/history' in paths
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_web_entrypoint.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.runtime.web`

- [ ] **Step 3: Write minimal implementation**

在 `gridtrade/config.py` 的 `DeployConfig` 末尾（`scheduler_run_on_start` 之后）追加字段：

```python
    dashboard_user: str = 'admin'
    dashboard_password_hash: str = ''
    dashboard_session_secret: str = ''
    dashboard_port: int = 8080
```

在 `load_deploy_config` 的 `DeployConfig(...)` 构造里追加（`scheduler_run_on_start=...` 之后）：

```python
        dashboard_user=_s(env, 'DASHBOARD_USER', 'admin'),
        dashboard_password_hash=_s(env, 'DASHBOARD_PASSWORD_HASH', ''),
        dashboard_session_secret=_s(env, 'DASHBOARD_SESSION_SECRET', ''),
        dashboard_port=_i(env, 'PORT', 8080),
```

新建 `gridtrade/runtime/web.py`：

```python
# gridtrade/runtime/web.py
"""web 机入口（fly 第三进程）：组装只读 dashboard，起 uvicorn。绝不写库/写交易所。"""
import secrets

from gridtrade.config import load_deploy_config
from gridtrade.dashboard.app import create_app
from gridtrade.runtime.factory import build_runtime
from gridtrade.runtime.introspect import adapter_endpoint


def build_web_app(config=None):
    config = config or load_deploy_config()
    rt = build_runtime(config)
    secret = config.dashboard_session_secret or secrets.token_hex(32)
    return create_app(rt.store, rt.adapter,
                      username=config.dashboard_user,
                      password_hash=config.dashboard_password_hash,
                      session_secret=secret)


def main() -> None:   # composition root（不单测）
    import uvicorn
    config = load_deploy_config()
    app = build_web_app(config)
    print('[web] exchange=%s testnet=%s endpoint=%s port=%s'
          % (config.exchange, config.testnet,
             adapter_endpoint(build_runtime(config).adapter),
             config.dashboard_port), flush=True)
    uvicorn.run(app, host='0.0.0.0', port=config.dashboard_port)


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_web_entrypoint.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run full suite (regression guard)**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: PASS（既有 292 passed + 2 skipped + 本计划新增 dashboard 测试全绿）

- [ ] **Step 6: Commit**

```bash
git add gridtrade/runtime/web.py gridtrade/config.py tests/dashboard/test_web_entrypoint.py
git commit -m "feat(dashboard): web 进程入口 build_web_app + uvicorn main + config 字段"
```

---

### Task 9: fly 部署 — web process group + scale-to-zero

**Files:**
- Modify: `deploy/fly.toml`
- Modify: `deploy/Dockerfile`（仅加 `EXPOSE 8080` 注释性声明）
- Modify: `docs/STATUS.md`（§5 部署表加 web 进程一行）
- Modify: `deploy/DEPLOY.md`（加 dashboard secret 设置与访问说明）

**Interfaces:**
- 无代码接口。配置变更：`web` process group 跑 `python -m gridtrade.runtime.web`；`[http_service]` 配 `internal_port = 8080`、`auto_stop_machines = "stop"`、`auto_start_machines = true`、`min_machines_running = 0`、`force_https = true`。

> 说明：本任务是部署配置变更，没有可自动断言的单测；按下述步骤手工核对。`monitor`/`scheduler` 的 `[[vm]].processes` 不变（它们不能 scale-to-zero）。

- [ ] **Step 1: 在 `deploy/fly.toml` 的 `[processes]` 增加 web 行**

```toml
[processes]
  monitor = "python -m gridtrade.runtime.monitor"
  scheduler = "python -m gridtrade.runtime.scheduler"
  web = "python -m gridtrade.runtime.web"
```

- [ ] **Step 2: 在 `deploy/fly.toml` 增加 `[http_service]`（仅绑 web 进程，scale-to-zero）**

```toml
# web dashboard：scale-to-zero（有请求才起、空闲自动停）。monitor/scheduler 无 http_service、保持常驻。
[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0
  processes = ["web"]
```

- [ ] **Step 3: 给 web 进程分配一台 VM（追加 `[[vm]]` 或在现有 processes 列表加 web）**

把现有 `[[vm]].processes` 改为包含 web：

```toml
[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
  processes = ["monitor", "scheduler", "web"]
```

- [ ] **Step 4: `deploy/Dockerfile` 增加端口声明（文档性，fly 用 internal_port）**

在 `CMD` 行前加一行：

```dockerfile
EXPOSE 8080
```

- [ ] **Step 5: 校验 fly 配置语法**

Run: `fly config validate -c deploy/fly.toml`
Expected: `Configuration is valid`（若本机未装 flyctl，跳过此步，部署时在 CI/CD 校验）

- [ ] **Step 6: 文档 — `deploy/DEPLOY.md` 追加 dashboard 段**

追加一节，内容为：

```markdown
## Dashboard（web 进程，只读监控）

设置登录凭据（密码用本地生成的 pbkdf2 哈希，不在仓库存明文）：

    HASH=$(.venv/bin/python -c "from gridtrade.dashboard.auth import hash_password; print(hash_password('你的密码'))")
    fly secrets set -a gridtrade-hl DASHBOARD_USER=admin DASHBOARD_PASSWORD_HASH="$HASH" DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)"

访问：`fly open -a gridtrade-hl`（web 进程 scale-to-zero，首次访问有数秒冷启动）。
登录失败 5 次锁定 ≥ 1 小时（内存态，机器重启后清零）。
```

- [ ] **Step 7: 文档 — `docs/STATUS.md` §5 部署表加 web 进程行**

在 §5 两个进程描述处补充第三个：

```markdown
- **web**：fly 第三进程（scale-to-zero），FastAPI 只读 dashboard（系统健康/活跃网格/单网格明细/历史战绩）；登录鉴权；`auto_stop/auto_start`，空闲停到零。
```

- [ ] **Step 8: Commit**

```bash
git add deploy/fly.toml deploy/Dockerfile deploy/DEPLOY.md docs/STATUS.md
git commit -m "feat(dashboard): fly web process group + scale-to-zero + 部署文档"
```

---

## Self-Review

**Spec coverage（逐节核对 `2026-06-29-dashboard-design.md`）：**
- §2 核心决策 → 全覆盖：只读(全任务)、web 进程(T8/T9)、FastAPI+Jinja+HTMX(T7)、只读行情 adapter(T1/T2)、登录+锁定(T6/T7)、tag 聚合(T4)。✅
- §4.1 queries.py → T1–T4。✅
- §4.2 auth.py → T6。✅
- §4.3 runtime/web.py → T8。✅
- §4.4 新依赖 → T7 Step 1。✅
- §4.5 fly 配置 + scale-to-zero → T9。✅
- §5 四视图 → 健康(T1/模板) / 总览(T2) / 明细(T3) / 战绩(T4)，模板 T7。✅
- §6 鉴权安全（HttpOnly/Secure/SameSite）→ T7 `set_cookie`。✅
- §7 测试（双后端 TDD / FakeExchange 注入）→ 各任务用 `store` fixture + fake adapter。✅
- §8 分阶段：P1 范围与本计划一致；P2/P3 明确不在范围。✅
- §9 开放项：心跳阈值默认 30s（T1，可 env 调）；密码哈希 pbkdf2 标准库(T6)；现价退化(T2)；锁定内存态单实例(T6/T9 文档注明)。✅

**Placeholder scan：** 无 TBD/TODO；每个 code step 给出完整代码与命令。`htmx.min.js` 为占位文件，已注明本期用 meta refresh 轮询、HTMX 局部刷新留 P3（非计划缺口，是明确的范围决策）。✅

**Type consistency：** `build_health`/`build_overview`/`build_grid_detail`/`build_records` 签名在 T1–T4 定义、T7 `app.py` 一致调用；`create_app` 签名 T7 定义、T8 一致调用；`LoginThrottle`/`make_session`/`verify_session`/`verify_password` 在 T6 定义、T7 一致使用；cookie 名 `gt_session` 在 T7 内自洽。✅
