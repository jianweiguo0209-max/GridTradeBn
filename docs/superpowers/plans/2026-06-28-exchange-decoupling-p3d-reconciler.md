# 交易所解耦重构 P3d 实现计划（对账自愈 Reconciler + 监控步 + 幂等成交）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 P3c 执行器上线前必需的稳态/恢复层：① 成交摄入按 `trade_id` 幂等（持久 `grid_fills` 表，杜绝同毫秒漏单与重启重复摄入/重复补单）；② `Reconciler.restore` 进程重启后为 DB 中 ACTIVE 网格重建执行器内存态（几何/序号/游标 + 用 `LiveEquity.replay` 重放成交）；③ `Reconciler.reconcile_open_orders` 按 `client_oid` 对账交易所真实挂单 vs `grid_orders`（撤孤儿、补缺失）；④ `monitor_grid` 监控步（sync→bump_peak→evaluate_exit→触发则 close）。

**Architecture:** 在 P2 状态层加 `grid_fills`（trade_id 主键）作为幂等去重 + 重放真相源。GridExecutor.sync 改为：成交先经 `FillRepository.add_if_new` 去重，只处理新成交（record_fill + 标记成交单 closed + 补对侧 + 持久化 fill）；游标从 `grid_fills` 派生（max ts），内存游标不再是正确性关键。`Reconciler` 是独立类（持 executor 引用）：`restore` 重建内存态（原型已验证 replay 重建后 net/realized 与重启前逐值一致），`reconcile_open_orders` 用 `client_oid` 集合做 diff。`monitor_grid` 是纯函数式一步（循环/调度归 P4）。

**Tech Stack:** Python 3.9、SQLAlchemy 2.0、ccxt 4.5.61、pandas 1.3.5、pytest。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（对账 diff 语义、重建口径、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；SQLAlchemy 2.0 Core 风格；测试针对 FakeExchange + 内存 SQLite，无外部网络。
- 幂等：同一 `trade_id` 只能被摄入一次（record_fill + 补单 + 记 fill 各一次）；重复 sync / 重启后重放都不得重复补单或重复记账。
- `grid_fills` 用 `trade_id` 主键实现去重；`FillRepository.add_if_new` 插入成功返回 True、已存在返回 False（捕获 IntegrityError）。
- 重启重建口径必须与 P3c open 同源：先按 `len(入场价严格上方线数)` 笔 `record_fill(entry,'buy',order_num, ts=0)` 重放中性底仓，再按 ts 升序重放 `grid_fills`。重建后 `LiveEquity.snapshot` 的 net_position/realized 必须与重启前逐值一致（原型已验证）。
- 订单对账：期望开仓集 = `grid_orders` 中 status='open' 的 client_oid 集合；交易所开仓集 = `fetch_open_orders` 的 client_oid 集合。孤儿（在所不在库）→ `cancel_order`；缺失（在库不在所）→ 用该订单参数重新 `create_limit_order`（沿用原 client_oid）并 upsert（保持 open）。
- `monitor_grid` 调 `evaluate_exit` 时，资金费已知 0 传 `funding_rate=0.0`（非 None）。
- `gridtrade/execution/` 与 `gridtrade/state/` 不得 import 交易所库（只经 `ExchangeAdapter`）。
- 不修改 `account_0/`、`backtest/`、`gridtrade/core/`、`gridtrade/exchanges/`、已有 `gridtrade/execution/live_equity.py`。本计划只新增/改 `gridtrade/state/{models,fills}.py`、`gridtrade/execution/{grid_executor,reconciler,monitor}.py` 及测试。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。

---

## 文件结构（本计划新建/修改）

```
gridtrade/state/
  models.py        # 修改：+ grid_fills 表 + Fill 数据类
  fills.py         # 新增：FillRepository（add_if_new / list_by_grid / max_ts）
gridtrade/execution/
  grid_executor.py # 修改：sync 改为按 trade_id 幂等（经 FillRepository）；+ FillRepository 注入
  reconciler.py    # 新增：Reconciler（restore / reconcile_open_orders）
  monitor.py       # 新增：monitor_grid（sync+bump_peak+evaluate_exit→close）
tests/state/test_fills.py
tests/execution/test_grid_executor_idempotent.py
tests/execution/test_reconciler.py
tests/execution/test_monitor.py
```

---

### Task 1: grid_fills 表 + FillRepository（幂等去重 + 重放源）

**Files:**
- Modify: `gridtrade/state/models.py`
- Create: `gridtrade/state/fills.py`
- Create: `tests/state/test_fills.py`

**Interfaces:**
- Produces:
  - `gridtrade.state.models.grid_fills`（Table）+ `Fill`（dataclass: `trade_id: str`(pk), `grid_id: str`, `line_index: int`, `side: str`, `price: float`, `size: float`, `ts: int`, `created_at: int`）。
  - `gridtrade.state.fills.FillRepository`：`__init__(self, store)`；`add_if_new(self, fill: Fill) -> bool`（trade_id 已存在返回 False，否则插入返回 True）；`list_by_grid(self, grid_id) -> List[Fill]`（按 ts 升序）；`max_ts(self, grid_id) -> int`（无则 0）。

- [ ] **Step 1: 写测试**

Create `tests/state/test_fills.py`:

```python
from gridtrade.state.models import Fill


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.fills import FillRepository
    s = StateStore.in_memory(); s.create_all()
    return FillRepository(s)


def _fill(trade_id='t1', **kw):
    base = dict(trade_id=trade_id, grid_id='g1', line_index=5, side='sell',
                price=100.5, size=0.5, ts=1000)
    base.update(kw)
    return Fill(**base)


def test_add_if_new_dedup():
    repo = _repo()
    assert repo.add_if_new(_fill('t1')) is True
    assert repo.add_if_new(_fill('t1')) is False    # 同 trade_id 第二次 → False
    assert len(repo.list_by_grid('g1')) == 1


def test_list_by_grid_sorted_by_ts():
    repo = _repo()
    repo.add_if_new(_fill('t3', ts=3000))
    repo.add_if_new(_fill('t1', ts=1000))
    repo.add_if_new(_fill('t2', ts=2000))
    assert [f.ts for f in repo.list_by_grid('g1')] == [1000, 2000, 3000]


def test_max_ts():
    repo = _repo()
    assert repo.max_ts('g1') == 0
    repo.add_if_new(_fill('t1', ts=1000))
    repo.add_if_new(_fill('t2', ts=5000))
    assert repo.max_ts('g1') == 5000
    assert repo.max_ts('other') == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_fills.py -v`
Expected: FAIL（`ImportError: cannot import name 'Fill'` 或 `ModuleNotFoundError: gridtrade.state.fills`）。

- [ ] **Step 3: 实现**

在 `gridtrade/state/models.py` 新增表（放在 order_records 之后）与数据类（放在 Record 之后）：
```python
grid_fills = Table(
    'grid_fills', metadata,
    Column('trade_id', String, primary_key=True),
    Column('grid_id', String, nullable=False),
    Column('line_index', Integer, nullable=False),
    Column('side', String, nullable=False),
    Column('price', Float, nullable=False),
    Column('size', Float, nullable=False),
    Column('ts', Integer, nullable=False),
    Column('created_at', Integer, nullable=False),
    Index('ix_grid_fills_grid', 'grid_id'),
)
```
```python
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
```

Create `gridtrade/state/fills.py`:
```python
"""FillRepository：已摄入成交的持久去重 + 重放真相源（trade_id 主键）。"""
from typing import List

import sqlalchemy as sa
from sqlalchemy import insert, select

from gridtrade.state.models import Fill, grid_fills, now_ms

_FIELDS = ('trade_id', 'grid_id', 'line_index', 'side', 'price', 'size', 'ts', 'created_at')


def _to_fill(row) -> Fill:
    m = row._mapping
    return Fill(**{f: m[f] for f in _FIELDS})


class FillRepository:
    def __init__(self, store):
        self.engine = store.engine

    def add_if_new(self, fill: Fill) -> bool:
        values = {f: getattr(fill, f) for f in _FIELDS}
        values['created_at'] = fill.created_at or now_ms()
        try:
            with self.engine.begin() as c:
                c.execute(insert(grid_fills), values)
            return True
        except sa.exc.IntegrityError:
            return False

    def list_by_grid(self, grid_id: str) -> List[Fill]:
        with self.engine.begin() as c:
            rows = c.execute(select(grid_fills)
                             .where(grid_fills.c.grid_id == grid_id)
                             .order_by(grid_fills.c.ts)).all()
        return [_to_fill(r) for r in rows]

    def max_ts(self, grid_id: str) -> int:
        with self.engine.begin() as c:
            v = c.execute(select(sa.func.max(grid_fills.c.ts))
                          .where(grid_fills.c.grid_id == grid_id)).scalar()
        return int(v) if v is not None else 0
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_fills.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/state/models.py gridtrade/state/fills.py tests/state/test_fills.py
git commit -m "feat(state): grid_fills table + FillRepository (idempotent fill record)"
```

---

### Task 2: GridExecutor.sync 改为按 trade_id 幂等（经 FillRepository）

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`
- Create: `tests/execution/test_grid_executor_idempotent.py`

**Interfaces:**
- Consumes: `gridtrade.state.fills.FillRepository`、`gridtrade.state.models.Fill`。
- Produces（改 GridExecutor）：
  - `__init__` 新增 `self.fills = FillRepository(store)`。
  - `sync` 改造：对每条候选成交先 `Fill(trade_id=t.id, grid_id, line_index, side, price, size, ts)` 经 `self.fills.add_if_new(fill)`；**仅当返回 True（新成交）才** record_fill + 标记成交单 closed + 补对侧单。重复成交（add_if_new False）跳过。`fetch_my_trades` 的 `since_ms` 改为从 `self.fills.max_ts(grid_id)` 派生（取 max_ts，无则 0），不再依赖内存 `_trade_cursor`（可保留但不作为正确性来源）。其余（资金费、snapshot、accounting）不变。返回 `{'new_fills': <新摄入数>, 'snapshot': snap}`。
  - 注：`Trade.id` 作为 trade_id（FakeExchange/ccxt 均提供）。

- [ ] **Step 1: 写测试**

Create `tests/execution/test_grid_executor_idempotent.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    store = StateStore.in_memory(); store.create_all()
    from gridtrade.execution.grid_executor import GridExecutor
    return ex, store, GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_fill_recorded_in_grid_fills():
    ex, store, gx = _setup()
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    fills = gx.fills.list_by_grid(gid)
    assert len(fills) == 1 and fills[0].side == 'sell'


def test_resync_same_trade_not_double_counted():
    # 即使游标被人为重置（模拟同毫秒/重复返回），trade_id 去重保证不重复摄入/补单
    ex, store, gx = _setup()
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6)
    r1 = gx.sync(gid, SYM)
    assert r1['new_fills'] == 1
    open_after_first = len(ex.fetch_open_orders(SYM))
    net_after_first = ex.fetch_positions(SYM).net_size
    # 强制重新拉取同一批成交：把内存游标清零（若存在）
    if hasattr(gx, '_trade_cursor'):
        gx._trade_cursor[gid] = 0
    r2 = gx.sync(gid, SYM)
    assert r2['new_fills'] == 0                       # 去重：无新成交
    assert len(ex.fetch_open_orders(SYM)) == open_after_first   # 未重复补单
    assert abs(ex.fetch_positions(SYM).net_size - net_after_first) < 1e-9
    assert len(gx.fills.list_by_grid(gid)) == 1       # 仍只一条 fill


def test_snapshot_consistent_after_resync():
    ex, store, gx = _setup()
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    snap1 = gx.live[gid].snapshot(ex.fetch_price(SYM))
    if hasattr(gx, '_trade_cursor'):
        gx._trade_cursor[gid] = 0
    gx.sync(gid, SYM)
    snap2 = gx.live[gid].snapshot(ex.fetch_price(SYM))
    assert abs(snap1['net_position'] - snap2['net_position']) < 1e-9
    assert abs(snap1['realized_pnl'] - snap2['realized_pnl']) < 1e-9
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor_idempotent.py -v`
Expected: FAIL（`AttributeError: ... 'fills'` 或重复摄入断言失败）。

- [ ] **Step 3: 改造 grid_executor.py**

import 顶部补：`from gridtrade.state.fills import FillRepository`、`from gridtrade.state.models import Fill`（与既有 models import 合并）。`__init__` 加 `self.fills = FillRepository(store)`。

把 `sync` 中"对每条新成交"的循环改为以 trade_id 去重驱动。将原先：
```python
        new = [t for t in trades
               if t.client_oid.startswith(prefix) and ':init:' not in t.client_oid]
        new.sort(key=lambda t: t.ts)

        for t in new:
            line_index = int(t.client_oid.split(':')[1])
            self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts)
            ...（标记 closed + 补对侧）...
        if new:
            self._trade_cursor[grid_id] = new[-1].ts + 1
```
替换为：
```python
        candidates = [t for t in trades
                      if t.client_oid.startswith(prefix) and ':init:' not in t.client_oid]
        candidates.sort(key=lambda t: t.ts)

        new_count = 0
        for t in candidates:
            line_index = int(t.client_oid.split(':')[1])
            fill = Fill(trade_id=str(t.id), grid_id=grid_id, line_index=line_index,
                        side=t.side, price=float(t.price), size=float(t.size), ts=int(t.ts))
            if not self.fills.add_if_new(fill):
                continue   # 已摄入：去重，跳过（不重复记账/补单）
            new_count += 1
            self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts)
            self.orders.upsert(GridOrder(client_oid=t.client_oid, grid_id=grid_id,
                                         line_index=line_index, side=t.side, price=t.price,
                                         size=t.size, status='closed'))
            opp_line = line_index - 1 if t.side == 'sell' else line_index + 1
            if 0 <= opp_line < len(price_array):
                opp_side = 'buy' if t.side == 'sell' else 'sell'
                p = price_array[opp_line]
                oid = self._next_oid(grid_id, opp_line)
                order = self.adapter.create_limit_order(symbol, opp_side, p, order_num,
                                                        post_only=False, client_oid=oid)
                self.orders.upsert(GridOrder(client_oid=oid, grid_id=grid_id, line_index=opp_line,
                                             side=opp_side, price=p, size=order_num, status='open',
                                             exchange_order_id=getattr(order, 'id', None)))
```
并把成交拉取游标改为从持久 fills 派生：将 `cursor = self._trade_cursor.get(grid_id, 0)` 改为 `cursor = self.fills.max_ts(grid_id)`。返回值 `'new_fills': new_count`。（`_trade_cursor` 可保留以兼容旧测试，但不再作为正确性来源。）

- [ ] **Step 4: 运行确认通过（含既有执行器测试不回归）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/ -v`
Expected: PASS（新增 3 + 既有 grid_executor/live_equity 全绿）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/grid_executor.py tests/execution/test_grid_executor_idempotent.py
git commit -m "feat(execution): idempotent fill ingestion via FillRepository (trade_id dedup)"
```

---

### Task 3: Reconciler.restore（重启重建执行器内存态）

**Files:**
- Create: `gridtrade/execution/reconciler.py`
- Create: `tests/execution/test_reconciler.py`

**Interfaces:**
- Consumes: `GridExecutor`（其 `grids/fills/_geom/_seq/_trade_cursor/_funding_cursor/live/cap/fee/c_rate_taker/max_rate`）、`core.grid_engine.grid_order_info`、`LiveEquity`。
- Produces: `gridtrade.execution.reconciler.Reconciler`：
  - `__init__(self, executor)`
  - `restore(self, grid_id) -> None`：从 DB 重建 executor 对该 grid 的内存态：读 `Grid`；`grid_order_info` 重算几何写 `_geom`；`_seq[gid]=itertools.count(10_000_000)`（避免与历史 client_oid seq 撞，重建后新补单用高位 seq）；新建 `LiveEquity(cap,fee,c_rate_taker,entry_price=entry)` 并重放：先 `len(入场价上方线数)` 笔 `record_fill(entry,'buy',order_num,0)`，再按 ts 升序对 `fills.list_by_grid(gid)` 逐笔 `record_fill`；写 `live[gid]`；`_trade_cursor[gid]=fills.max_ts(gid)`、`_funding_cursor[gid]=0`（资金费重放留待 sync 增量补，P3d 不重算历史资金费——记为已知近似）。

- [ ] **Step 1: 写测试**

Create `tests/execution/test_reconciler.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _new_executor(ex, store):
    from gridtrade.execution.grid_executor import GridExecutor
    return GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_restore_rebuilds_state_matching_pre_restart():
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    store = StateStore.in_memory(); store.create_all()
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6); gx.sync(gid, SYM)
    snap_before = gx.live[gid].snapshot(ex.fetch_price(SYM))

    # 模拟重启：全新 executor（空内存），共享同一 store/exchange
    gx2 = _new_executor(ex, store)
    assert gid not in gx2.live
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)
    # 重建后内存态可用且与重启前一致
    assert gid in gx2.live and gid in gx2._geom
    snap_after = gx2.live[gid].snapshot(ex.fetch_price(SYM))
    assert abs(snap_before['net_position'] - snap_after['net_position']) < 1e-9
    assert abs(snap_before['realized_pnl'] - snap_after['realized_pnl']) < 1e-9


def test_restore_then_sync_no_double_replenish():
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    store = StateStore.in_memory(); store.create_all()
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6); gx.sync(gid, SYM)
    open_before = len(ex.fetch_open_orders(SYM))

    gx2 = _new_executor(ex, store)
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)
    res = gx2.sync(gid, SYM)             # 重启后 sync：历史成交已在 grid_fills → 不重复摄入
    assert res['new_fills'] == 0
    assert len(ex.fetch_open_orders(SYM)) == open_before
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_reconciler.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.reconciler`）。

- [ ] **Step 3: 写 reconciler.py（restore 部分）**

Create `gridtrade/execution/reconciler.py`:
```python
"""Reconciler：重启对账自愈。restore 重建执行器内存态；reconcile_open_orders 按 client_oid 对账。"""
import itertools

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.live_equity import LiveEquity


class Reconciler:
    def __init__(self, executor):
        self.ex = executor

    def restore(self, grid_id):
        ex = self.ex
        g = ex.grids.get(grid_id)
        if g is None:
            raise ValueError('grid %s not found' % grid_id)
        gi = grid_order_info(ex.cap, ex.leverage, g.low_price, g.high_price,
                             int(g.grid_count), g.stop_low_price, g.stop_high_price,
                             min_amount=ex.min_amount, max_rate=ex.max_rate)
        price_array = [float(p) for p in gi['价格序列']]
        order_num = float(gi['每笔数量'])
        ex._geom[grid_id] = {'price_array': price_array, 'order_num': order_num}
        ex._seq[grid_id] = itertools.count(10_000_000)  # 高位起，避免与历史 seq 相撞

        live = LiveEquity(ex.cap, ex.fee, ex.c_rate_taker, entry_price=g.entry_price)
        above = [p for p in price_array if p > g.entry_price]
        for _ in range(len(above)):
            live.record_fill(g.entry_price, 'buy', order_num, 0)
        for f in ex.fills.list_by_grid(grid_id):   # 已按 ts 升序
            live.record_fill(f.price, f.side, f.size, f.ts)
        ex.live[grid_id] = live
        ex._trade_cursor[grid_id] = ex.fills.max_ts(grid_id)
        ex._funding_cursor[grid_id] = 0
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_reconciler.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/reconciler.py tests/execution/test_reconciler.py
git commit -m "feat(execution): Reconciler.restore (rebuild executor state via LiveEquity replay)"
```

---

### Task 4: Reconciler.reconcile_open_orders（按 client_oid 对账挂单）

**Files:**
- Modify: `gridtrade/execution/reconciler.py`
- Modify: `tests/execution/test_reconciler.py`

**Interfaces:**
- Produces（在 Reconciler 新增）：
  - `reconcile_open_orders(self, grid_id, symbol) -> dict`：
    - 期望开仓 = `executor.orders.list_open_by_grid(grid_id)`（按 client_oid 建 dict）。
    - 交易所开仓 = `adapter.fetch_open_orders(symbol)`（client_oid 集合）。
    - 孤儿（在所不在期望集）：`adapter.cancel_order(symbol, order.id)`。
    - 缺失（在期望集不在所）：用该 GridOrder 重新 `adapter.create_limit_order(symbol, side, price, size, post_only=False, client_oid=同一 client_oid)`，并 upsert（status 仍 open，更新 exchange_order_id）。
    - 返回 `{'canceled': n_orphan, 'replaced': n_missing}`。

- [ ] **Step 1: 追加测试**

```python
def test_reconcile_cancels_orphan_and_replaces_missing():
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    store = StateStore.in_memory(); store.create_all()
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    from gridtrade.execution.reconciler import Reconciler
    rec = Reconciler(gx)

    # 干净状态：无孤儿无缺失
    out0 = rec.reconcile_open_orders(gid, SYM)
    assert out0 == {'canceled': 0, 'replaced': 0}

    # 制造缺失：直接在交易所撤掉一个挂单（DB 仍记 open）
    victim = ex.fetch_open_orders(SYM)[0]
    ex.cancel_order(SYM, victim.id)
    assert len(ex.fetch_open_orders(SYM)) == 8
    out1 = rec.reconcile_open_orders(gid, SYM)
    assert out1['replaced'] == 1 and out1['canceled'] == 0
    assert len(ex.fetch_open_orders(SYM)) == 9       # 已补回

    # 制造孤儿：交易所多挂一个不属于本网格意图的单
    ex.create_limit_order(SYM, 'buy', 95.0, 0.5, client_oid='zzz:orphan:0')
    out2 = rec.reconcile_open_orders(gid, SYM)
    assert out2['canceled'] == 1
    assert all(o.client_oid != 'zzz:orphan:0' for o in ex.fetch_open_orders(SYM))
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_reconciler.py -k reconcile -v`
Expected: FAIL（`AttributeError: ... 'reconcile_open_orders'`）。

- [ ] **Step 3: 实现 reconcile_open_orders**

import 顶部补 `from gridtrade.state.models import GridOrder`。在 Reconciler 新增：
```python
    def reconcile_open_orders(self, grid_id, symbol):
        ex = self.ex
        expected = {o.client_oid: o for o in ex.orders.list_open_by_grid(grid_id)}
        on_exchange = {o.client_oid: o for o in ex.adapter.fetch_open_orders(symbol)}

        canceled = 0
        for coid, o in on_exchange.items():
            if coid not in expected:
                ex.adapter.cancel_order(symbol, o.id)
                canceled += 1

        replaced = 0
        for coid, go in expected.items():
            if coid not in on_exchange:
                order = ex.adapter.create_limit_order(symbol, go.side, go.price, go.size,
                                                      post_only=False, client_oid=coid)
                ex.orders.upsert(GridOrder(client_oid=coid, grid_id=grid_id,
                                           line_index=go.line_index, side=go.side, price=go.price,
                                           size=go.size, status='open',
                                           exchange_order_id=getattr(order, 'id', None)))
                replaced += 1
        return {'canceled': canceled, 'replaced': replaced}
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_reconciler.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/reconciler.py tests/execution/test_reconciler.py
git commit -m "feat(execution): Reconciler.reconcile_open_orders (cancel orphans, replace missing by client_oid)"
```

---

### Task 5: monitor_grid 监控步（sync+bump_peak+evaluate_exit→close）+ 全套回归

**Files:**
- Create: `gridtrade/execution/monitor.py`
- Create: `tests/execution/test_monitor.py`

**Interfaces:**
- Consumes: `GridExecutor`、`core.stop_rules.evaluate_exit`。
- Produces: `gridtrade.execution.monitor.monitor_grid(executor, grid_id, symbol, stop_cfg, *, margin_rate=0.05) -> dict`：
  - `res = executor.sync(grid_id, symbol)`；`snap = res['snapshot']`。
  - 读 `acc = executor.accounting.get(grid_id)`；`pnl_ratio_max = acc.pnl_ratio_max if acc else snap['pnl_ratio']`。
  - `reason = evaluate_exit(snap['pnl_ratio'], pnl_ratio_max, net_value=snap['net_value'], stop_cfg=stop_cfg, margin_rate=margin_rate, funding_rate=0.0, pv_spike=0)`。
  - 若 `reason`：`executor.close(grid_id, symbol, reason)`；返回 `{'closed': True, 'reason': reason, 'pnl_ratio': snap['pnl_ratio']}`。否则 `{'closed': False, 'reason': None, 'pnl_ratio': snap['pnl_ratio']}`。
  - （资金费率/pv 的实盘接入留待后续；本步按 spec 用 funding_rate=0.0、pv_spike=0，固定止损/回撤/爆仓即可生效。循环调度归 P4。）

- [ ] **Step 1: 写测试**

Create `tests/execution/test_monitor.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
from gridtrade.state.models import CLOSED

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    store = StateStore.in_memory(); store.create_all()
    from gridtrade.execution.grid_executor import GridExecutor
    return ex, store, GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_monitor_no_exit_when_flat_pnl():
    from gridtrade.execution.monitor import monitor_grid
    ex, store, gx = _setup(100.0)
    gid = gx.open('fake', SYM, GP)
    out = monitor_grid(gx, gid, SYM, STOP_CFG)
    assert out['closed'] is False and out['reason'] is None


def test_monitor_triggers_fixed_stop_and_closes():
    from gridtrade.execution.monitor import monitor_grid
    from gridtrade.state.grids import GridRepository
    ex, store, gx = _setup(100.0)
    gid = gx.open('fake', SYM, GP)
    # 价格大跌：中性底仓多头浮亏，pnl_ratio 跌破 -3.4% → 固定止损
    ex.set_price(SYM, 96.5)
    out = monitor_grid(gx, gid, SYM, STOP_CFG)
    assert out['closed'] is True and out['reason'] == '固定止损'
    assert GridRepository(store).get(gid).status == CLOSED
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9   # 已平
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_monitor.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.monitor`）。

> 备注：若 `test_monitor_triggers_fixed_stop_and_closes` 的具体触发价（96.5）实测未跌破 -3.4%（取决于中性底仓量与杠杆），实现者应**先实测该网格在何价位 pnl_ratio<-0.034**（用 `gx.live[gid].snapshot(price)` 试几个价），再把测试价改为确实触发的值——这是校准测试输入，非改 monitor 逻辑。不确定则问。

- [ ] **Step 3: 写 monitor.py**

Create `gridtrade/execution/monitor.py`:
```python
"""monitor_grid：单网格监控步（sync → 评估退出 → 触发则平仓）。循环/调度归 P4 运行时。"""
from gridtrade.core.stop_rules import evaluate_exit


def monitor_grid(executor, grid_id, symbol, stop_cfg, *, margin_rate=0.05):
    res = executor.sync(grid_id, symbol)
    snap = res['snapshot']
    acc = executor.accounting.get(grid_id)
    pnl_ratio_max = acc.pnl_ratio_max if acc is not None else snap['pnl_ratio']
    reason = evaluate_exit(snap['pnl_ratio'], pnl_ratio_max, net_value=snap['net_value'],
                           stop_cfg=stop_cfg, margin_rate=margin_rate,
                           funding_rate=0.0, pv_spike=0)
    if reason:
        executor.close(grid_id, symbol, reason)
        return {'closed': True, 'reason': reason, 'pnl_ratio': snap['pnl_ratio']}
    return {'closed': False, 'reason': None, 'pnl_ratio': snap['pnl_ratio']}
```

- [ ] **Step 4: 运行确认通过 + 全套回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_monitor.py -v`
Expected: PASS（2 passed）。

Run（全仓回归）: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全 PASS（既有 86 + 本计划新增 ≈ 13）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/monitor.py tests/execution/test_monitor.py
git commit -m "feat(execution): monitor_grid step (sync + evaluate_exit -> close)"
```

---

## 完成判定（P3d）

- `pytest -q` 全绿：grid_fills 幂等；sync 按 trade_id 去重（重复 sync/游标重置不重复摄入或补单）；Reconciler.restore 重建后 snapshot 与重启前逐值一致且 sync 不重复摄入；reconcile_open_orders 撤孤儿/补缺失；monitor_grid 触发固定止损并平仓释放槽位。
- 全程 FakeExchange + 内存 SQLite，无外部网络。
- 至此 P3 全链路（引擎/记账/执行/对账/监控步）离线闭环；**实盘上线还需 P4 运行时**（scheduler/monitor 进程 + fly.io + 真实 Postgres/对象存储 + secrets）。

## 后续（P4，不在本计划内，需用户基础设施决策）

P4 运行时：scheduler 机（每小时触发选币+再平衡，复用主流程）+ monitor 机（轮询 ACTIVE 网格跑 `monitor_grid`，崩溃后用 `Reconciler.restore`+`reconcile_open_orders` 自愈）+ fly.io 部署（Dockerfile/fly.toml/多 process group）+ 真实 Postgres（应用 P4 carry-forward：读用 connect()、transition 事务内重校验）+ 对象存储 + secrets。**P4 开始前需与用户确认**：fly.io 账号/组织、Postgres 与对象存储如何 provision、secrets 注入方式、监控轮询间隔。
