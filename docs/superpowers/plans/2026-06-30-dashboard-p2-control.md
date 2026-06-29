# Dashboard 第二期（控制台）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给已上线的只读 Dashboard 加控制能力——kill 两档、关/开网格、暂停 scheduler、查看候选币池——全部经 DB 指令队列由 monitor 执行（web 永不下单），并把 UI 做成手机响应式。

**Architecture:** web 进程把控制动作写成 Postgres 的标志位（`control_flags`）或指令（`control_commands`），并记审计（`control_audit`）；monitor 进程每周期读标志门控加仓类动作、认领并执行一条指令、写回结果+审计；scheduler 读标志门控选币。真正动交易所的执行只在 monitor，沿用现有 `executor`/`manager` 与乐观锁/幂等/per-grid 隔离。

**Tech Stack:** Python 3.9 / FastAPI / Jinja2 / HTMX / SQLAlchemy 2.0 Core / pytest（双后端 fixture）。

## Global Constraints

- Python 3.9；测试命令 `TZ=Asia/Shanghai .venv/bin/python -m pytest`。
- DB 测试用 `tests/conftest.py` 的 `store` fixture（默认内存 SQLite；`TEST_DATABASE_URL` 有值走 PG）。
- 时间戳一律 UTC 毫秒整数，用 `gridtrade.state.models.now_ms`。
- **web 进程零下单/零平仓**：dashboard 控制路由只写 `control_*` 三表 + 只读 `fetch_price`/`fetch_candles`/`fetch_balance`；真正下单/平仓只在 monitor。
- 标志缺行视为 `false`。指令认领用乐观锁版本守卫（沿用 `grids.transition_status` 套路），保证一条指令至多执行一次。
- halt 语义：`trading_halted=true` 冻结**加仓类**（replenish 补单 / open 开仓 / scheduler 选币）；**止损平仓 / reconcile / accounting 照常**。
- 新增交易所写调用一律经现有 `executor`；FakeExchange 注入用于测试，不碰真交易所。
- 控制路由全在 P1 登录会话之后（沿用 `app.py` 的 `_user`），写动作全为 POST。

---

### Task 1: 状态层三张控制表 + 数据类

**Files:**
- Modify: `gridtrade/state/models.py`（在 `heartbeats` 表后追加 3 表；在数据类区追加 3 dataclass）
- Test: `tests/state/test_control_models.py`

**Interfaces:**
- Consumes: `gridtrade.state.models`（`metadata`, `now_ms`, `BigInteger` 等已 import）。
- Produces:
  - 表对象：`control_flags`、`control_commands`、`control_audit`（`metadata` 注册）。
  - dataclass：
    - `ControlFlag(name: str, value: str, updated_at: int = 0, updated_by: str = '')`
    - `ControlCommand(id: str, type: str, payload: str, status: str = 'PENDING', result: Optional[str] = None, created_at: int = 0, created_by: str = '', claimed_at: Optional[int] = None, finished_at: Optional[int] = None, version: int = 1)`
    - `AuditEntry(id: str, ts: int, actor: str, action: str, target: str, detail: str = '', outcome: str = 'ok')`
  - 状态常量：`CMD_PENDING='PENDING'`, `CMD_RUNNING='RUNNING'`, `CMD_DONE='DONE'`, `CMD_FAILED='FAILED'`。

- [ ] **Step 1: Write the failing test**

```python
# tests/state/test_control_models.py
from gridtrade.state.models import (control_flags, control_commands, control_audit,
                                    ControlFlag, ControlCommand, AuditEntry,
                                    CMD_PENDING, metadata)


def test_control_tables_registered_in_metadata():
    names = set(metadata.tables)
    assert {'control_flags', 'control_commands', 'control_audit'} <= names


def test_control_dataclasses_defaults():
    f = ControlFlag(name='trading_halted', value='true')
    assert f.updated_at == 0 and f.updated_by == ''
    c = ControlCommand(id='c1', type='CLOSE_GRID', payload='{}')
    assert c.status == CMD_PENDING and c.version == 1 and c.result is None
    a = AuditEntry(id='a1', ts=1, actor='admin', action='FLAG_SET', target='trading_halted')
    assert a.outcome == 'ok' and a.detail == ''
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_control_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'control_flags'`

- [ ] **Step 3: Write minimal implementation**

在 `gridtrade/state/models.py` 的 `heartbeats = Table(...)` 定义之后追加：

```python
# ---- 控制面（dashboard 第二期）----
CMD_PENDING = 'PENDING'
CMD_RUNNING = 'RUNNING'
CMD_DONE = 'DONE'
CMD_FAILED = 'FAILED'

control_flags = Table(
    'control_flags', metadata,
    Column('name', String, primary_key=True),
    Column('value', String, nullable=False),
    Column('updated_at', BigInteger, nullable=False, default=0),
    Column('updated_by', String, nullable=False, default=''),
)

control_commands = Table(
    'control_commands', metadata,
    Column('id', String, primary_key=True),
    Column('type', String, nullable=False),
    Column('payload', String, nullable=False, default='{}'),
    Column('status', String, nullable=False, default=CMD_PENDING),
    Column('result', String, nullable=True),
    Column('created_at', BigInteger, nullable=False),
    Column('created_by', String, nullable=False, default=''),
    Column('claimed_at', BigInteger, nullable=True),
    Column('finished_at', BigInteger, nullable=True),
    Column('version', Integer, nullable=False, default=1),
    Index('ix_control_commands_status', 'status'),
)

control_audit = Table(
    'control_audit', metadata,
    Column('id', String, primary_key=True),
    Column('ts', BigInteger, nullable=False),
    Column('actor', String, nullable=False, default=''),
    Column('action', String, nullable=False),
    Column('target', String, nullable=False, default=''),
    Column('detail', String, nullable=False, default=''),
    Column('outcome', String, nullable=False, default='ok'),
    Index('ix_control_audit_ts', 'ts'),
)
```

在数据类区（文件末尾 `@dataclass class Heartbeat` 之后）追加：

```python
@dataclass
class ControlFlag:
    name: str
    value: str
    updated_at: int = 0
    updated_by: str = ''


@dataclass
class ControlCommand:
    id: str
    type: str
    payload: str
    status: str = CMD_PENDING
    result: Optional[str] = None
    created_at: int = 0
    created_by: str = ''
    claimed_at: Optional[int] = None
    finished_at: Optional[int] = None
    version: int = 1


@dataclass
class AuditEntry:
    id: str
    ts: int
    actor: str
    action: str
    target: str
    detail: str = ''
    outcome: str = 'ok'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_control_models.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/state/models.py tests/state/test_control_models.py
git commit -m "feat(control): 状态层三张控制表 + 数据类（flags/commands/audit）"
```

---

### Task 2: ControlFlagRepository（标志位读写，缺行默认 false）

**Files:**
- Create: `gridtrade/state/control.py`
- Test: `tests/state/test_control_flags_repo.py`

**Interfaces:**
- Consumes: `store.engine`；`gridtrade.state.models`（`control_flags`, `ControlFlag`, `now_ms`）。
- Produces:
  - `class ControlFlagRepository(store)`：
    - `get(name: str) -> bool`（缺行返回 `False`；存在按 `value == 'true'`）
    - `set(name: str, value: bool, *, actor: str = '') -> None`（upsert，写 `updated_at`/`updated_by`）

- [ ] **Step 1: Write the failing test**

```python
# tests/state/test_control_flags_repo.py
from gridtrade.state.control import ControlFlagRepository


def test_flag_defaults_false_then_set_toggle(store):
    flags = ControlFlagRepository(store)
    assert flags.get('trading_halted') is False        # 缺行默认 false
    flags.set('trading_halted', True, actor='admin')
    assert flags.get('trading_halted') is True
    flags.set('trading_halted', False, actor='admin')
    assert flags.get('trading_halted') is False        # upsert 覆盖
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_control_flags_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.state.control`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/state/control.py
"""控制面仓储：标志位 / 指令队列 / 审计。引擎无关，沿用 state 层乐观锁风格。"""
import sqlalchemy as sa
from sqlalchemy import insert, select, update

from gridtrade.state.models import control_flags, now_ms


class ControlFlagRepository:
    def __init__(self, store):
        self.engine = store.engine

    def get(self, name: str) -> bool:
        with self.engine.connect() as c:
            row = c.execute(
                select(control_flags.c.value).where(control_flags.c.name == name)
            ).first()
        return bool(row is not None and row[0] == 'true')

    def set(self, name: str, value: bool, *, actor: str = '') -> None:
        v = 'true' if value else 'false'
        ts = now_ms()
        try:
            with self.engine.begin() as c:
                c.execute(insert(control_flags),
                          {'name': name, 'value': v, 'updated_at': ts,
                           'updated_by': actor})
        except sa.exc.IntegrityError:
            with self.engine.begin() as c:
                c.execute(update(control_flags)
                          .where(control_flags.c.name == name)
                          .values(value=v, updated_at=ts, updated_by=actor))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_control_flags_repo.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/state/control.py tests/state/test_control_flags_repo.py
git commit -m "feat(control): ControlFlagRepository（缺行默认 false + upsert）"
```

---

### Task 3: CommandRepository（入队 / 版本守卫认领 / 终态）

**Files:**
- Modify: `gridtrade/state/control.py`
- Test: `tests/state/test_command_repo.py`

**Interfaces:**
- Consumes: `gridtrade.state.models`（`control_commands`, `ControlCommand`, `CMD_PENDING/RUNNING/DONE/FAILED`, `now_ms`, `ConcurrencyError`）。
- Produces（追加到 `control.py`）：
  - `class CommandRepository(store)`：
    - `enqueue(type: str, payload: str, *, created_by: str = '') -> ControlCommand`（生成 uuid id，status=PENDING）
    - `claim_next() -> Optional[ControlCommand]`（取最早 PENDING，版本守卫 PENDING→RUNNING 设 `claimed_at`；并发下未抢到返回 None / 重试一次）
    - `finish(command_id: str, status: str, result: str) -> None`（RUNNING→DONE/FAILED 设 `finished_at`/`result`）
    - `list_recent(limit: int = 50) -> List[ControlCommand]`（按 created_at 降序）

- [ ] **Step 1: Write the failing test**

```python
# tests/state/test_command_repo.py
from gridtrade.state.control import CommandRepository
from gridtrade.state.models import CMD_RUNNING, CMD_DONE, CMD_PENDING


def test_enqueue_claim_finish_cycle(store):
    repo = CommandRepository(store)
    c = repo.enqueue('CLOSE_GRID', '{"grid_id": "g1"}', created_by='admin')
    assert c.status == CMD_PENDING and c.id

    claimed = repo.claim_next()
    assert claimed.id == c.id and claimed.status == CMD_RUNNING
    assert claimed.claimed_at is not None

    assert repo.claim_next() is None         # 已无 PENDING

    repo.finish(c.id, CMD_DONE, 'closed ok')
    recent = repo.list_recent()
    done = [x for x in recent if x.id == c.id][0]
    assert done.status == CMD_DONE and done.result == 'closed ok'
    assert done.finished_at is not None


def test_claim_is_fifo(store):
    repo = CommandRepository(store)
    a = repo.enqueue('CLOSE_GRID', '{}', created_by='admin')
    b = repo.enqueue('CLOSE_GRID', '{}', created_by='admin')
    assert repo.claim_next().id == a.id      # 先进先出
    assert repo.claim_next().id == b.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_command_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'CommandRepository'`

- [ ] **Step 3: Write minimal implementation** (append to `gridtrade/state/control.py`)

```python
# --- 追加到 gridtrade/state/control.py ---
import uuid
from typing import List, Optional

from gridtrade.state.models import (control_commands, ControlCommand, CMD_PENDING,
                                    CMD_RUNNING, now_ms)

_CMD_FIELDS = ('id', 'type', 'payload', 'status', 'result', 'created_at',
               'created_by', 'claimed_at', 'finished_at', 'version')


def _to_cmd(row) -> ControlCommand:
    m = row._mapping
    return ControlCommand(**{f: m[f] for f in _CMD_FIELDS})


class CommandRepository:
    def __init__(self, store):
        self.engine = store.engine

    def enqueue(self, type: str, payload: str, *, created_by: str = '') -> ControlCommand:
        cid = uuid.uuid4().hex
        ts = now_ms()
        with self.engine.begin() as c:
            c.execute(insert(control_commands), {
                'id': cid, 'type': type, 'payload': payload, 'status': CMD_PENDING,
                'result': None, 'created_at': ts, 'created_by': created_by,
                'claimed_at': None, 'finished_at': None, 'version': 1,
            })
        return self.get(cid)

    def get(self, command_id: str) -> Optional[ControlCommand]:
        with self.engine.connect() as c:
            row = c.execute(select(control_commands)
                            .where(control_commands.c.id == command_id)).first()
        return _to_cmd(row) if row is not None else None

    def claim_next(self) -> Optional[ControlCommand]:
        for _ in range(2):                       # 并发抢同一条时重试一次
            with self.engine.connect() as c:
                row = c.execute(
                    select(control_commands)
                    .where(control_commands.c.status == CMD_PENDING)
                    .order_by(control_commands.c.created_at, control_commands.c.id)
                    .limit(1)
                ).first()
            if row is None:
                return None
            cmd = _to_cmd(row)
            with self.engine.begin() as c:
                res = c.execute(
                    update(control_commands)
                    .where(control_commands.c.id == cmd.id,
                           control_commands.c.version == cmd.version)
                    .values(status=CMD_RUNNING, claimed_at=now_ms(),
                            version=cmd.version + 1)
                )
            if res.rowcount == 1:
                return self.get(cmd.id)
        return None

    def finish(self, command_id: str, status: str, result: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(control_commands)
                      .where(control_commands.c.id == command_id)
                      .values(status=status, result=result, finished_at=now_ms()))

    def list_recent(self, limit: int = 50) -> List[ControlCommand]:
        with self.engine.connect() as c:
            rows = c.execute(select(control_commands)
                             .order_by(control_commands.c.created_at.desc())
                             .limit(limit)).all()
        return [_to_cmd(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_command_repo.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/state/control.py tests/state/test_command_repo.py
git commit -m "feat(control): CommandRepository（enqueue/版本守卫 claim/finish/list）"
```

---

### Task 4: AuditRepository（审计日志 add/list）

**Files:**
- Modify: `gridtrade/state/control.py`
- Test: `tests/state/test_audit_repo.py`

**Interfaces:**
- Consumes: `gridtrade.state.models`（`control_audit`, `AuditEntry`, `now_ms`）。
- Produces（追加到 `control.py`）：
  - `class AuditRepository(store)`：
    - `add(actor: str, action: str, target: str, *, detail: str = '', outcome: str = 'ok') -> AuditEntry`（生成 uuid id + `ts=now_ms()`）
    - `list_recent(limit: int = 100) -> List[AuditEntry]`（按 ts 降序）

- [ ] **Step 1: Write the failing test**

```python
# tests/state/test_audit_repo.py
from gridtrade.state.control import AuditRepository


def test_audit_add_and_list_recent(store):
    a = AuditRepository(store)
    a.add('admin', 'FLAG_SET', 'trading_halted', detail='{"value": true}')
    a.add('admin', 'CMD_SUBMIT', 'cmd1', detail='{"type": "CLOSE_GRID"}')
    rows = a.list_recent()
    assert len(rows) == 2
    assert rows[0].action in ('FLAG_SET', 'CMD_SUBMIT')   # 降序，两条都在
    assert all(r.actor == 'admin' and r.ts > 0 for r in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_audit_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'AuditRepository'`

- [ ] **Step 3: Write minimal implementation** (append to `gridtrade/state/control.py`)

```python
# --- 追加到 gridtrade/state/control.py ---
from gridtrade.state.models import control_audit, AuditEntry

_AUDIT_FIELDS = ('id', 'ts', 'actor', 'action', 'target', 'detail', 'outcome')


def _to_audit(row) -> AuditEntry:
    m = row._mapping
    return AuditEntry(**{f: m[f] for f in _AUDIT_FIELDS})


class AuditRepository:
    def __init__(self, store):
        self.engine = store.engine

    def add(self, actor: str, action: str, target: str, *,
            detail: str = '', outcome: str = 'ok') -> AuditEntry:
        aid = uuid.uuid4().hex
        ts = now_ms()
        with self.engine.begin() as c:
            c.execute(insert(control_audit), {
                'id': aid, 'ts': ts, 'actor': actor, 'action': action,
                'target': target, 'detail': detail, 'outcome': outcome,
            })
        with self.engine.connect() as c:
            row = c.execute(select(control_audit)
                            .where(control_audit.c.id == aid)).first()
        return _to_audit(row)

    def list_recent(self, limit: int = 100) -> List[AuditEntry]:
        with self.engine.connect() as c:
            rows = c.execute(select(control_audit)
                             .order_by(control_audit.c.ts.desc())
                             .limit(limit)).all()
        return [_to_audit(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_audit_repo.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/state/control.py tests/state/test_audit_repo.py
git commit -m "feat(control): AuditRepository（add/list_recent）"
```

---

### Task 5: executor.open 支持 cap 覆盖

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`（`open` 方法签名 + cap 取值）
- Test: `tests/execution/test_executor_open_cap.py`

**Interfaces:**
- Consumes: 现有 `GridExecutor.open(self, exchange, symbol, grid_params, *, offset=0, tag='')`。
- Produces: `GridExecutor.open(self, exchange, symbol, grid_params, *, offset=0, tag='', cap=None)`——`cap is None` 时用 `self.cap`（行为不变）；否则用传入 cap 计算 `grid_order_info` 并写入 `Grid.cap`。

- [ ] **Step 1: Write the failing test**

```python
# tests/execution/test_executor_open_cap.py
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.store import StateStore


def _executor():
    store = StateStore.in_memory(); store.create_all()
    ex = GridExecutor(FakeExchange(), store, cap=100.0, leverage=5.0)
    return ex


def test_open_uses_cap_override():
    ex = _executor()
    gp = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 10,
          'stop_low_price': 80.0, 'stop_high_price': 120.0}
    gid = ex.open('fake', 'BTC/USDT:USDT', gp, tag='gt0', cap=250.0)
    grid = ex.grids.get(gid)
    assert grid.cap == 250.0                  # 覆盖值写入网格

    gid2 = ex.open('fake', 'ETH/USDT:USDT', gp, tag='gt0')
    assert ex.grids.get(gid2).cap == 100.0    # 不传 cap → 用 self.cap（行为不变）
```

> 注：实现期若 FakeExchange 的 `open` 需要先 `set_leverage`/`fetch_price` 等准备，按 `tests/execution/` 既有 FakeExchange 用法补齐；本测试只断言 `grid.cap`。

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_executor_open_cap.py -v`
Expected: FAIL — `TypeError: open() got an unexpected keyword argument 'cap'`

- [ ] **Step 3: Write minimal implementation**

在 `gridtrade/execution/grid_executor.py` 改 `open`：签名加 `cap=None`，方法体首行解析 `cap = self.cap if cap is None else cap`，并把 `grid_order_info(self.cap, ...)` 与 `Grid(..., cap=self.cap)` 两处的 `self.cap` 改为局部 `cap`：

```python
    def open(self, exchange, symbol, grid_params, *, offset=0, tag='', cap=None):
        cap = self.cap if cap is None else cap
        gi = grid_order_info(cap, self.leverage, grid_params['low_price'],
                             grid_params['high_price'], int(grid_params['grid_count']),
                             grid_params['stop_low_price'], grid_params['stop_high_price'],
                             min_amount=self.min_amount, max_rate=self.max_rate)
        # ... 其余不变 ...
        grid = self.grids.create(Grid(
            id='', exchange=exchange, symbol=symbol, status='PENDING', offset=offset, tag=tag,
            entry_price=entry, low_price=grid_params['low_price'], high_price=grid_params['high_price'],
            stop_low_price=grid_params['stop_low_price'], stop_high_price=grid_params['stop_high_price'],
            grid_count=int(grid_params['grid_count']), order_num=order_num,
            leverage=self.leverage, cap=cap))
```

（只改 `grid_order_info(...)` 第一参数与 `Grid(..., cap=...)`，其余方法体保持原样。）

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_executor_open_cap.py -v`
然后跑既有执行层回归：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/ -q`
Expected: PASS（新测 2 passed；既有执行层测试不回归）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/execution/grid_executor.py tests/execution/test_executor_open_cap.py
git commit -m "feat(control): executor.open 支持 cap 覆盖（默认 self.cap，行为不变）"
```

---

### Task 6: 指令执行分发 execute_command（CLOSE/OPEN/PANIC）

**Files:**
- Create: `gridtrade/runtime/commands.py`
- Test: `tests/runtime/test_execute_command.py`

**Interfaces:**
- Consumes: `manager.executor`（`open(exchange,symbol,grid_params,*,offset,tag,cap)`, `close(grid_id,symbol,reason)`, `grids.list_active()`）；`gridtrade.state.models`（`ControlCommand`, `ACTIVE_STATES`）；标志读取 `flags.get('trading_halted')`。
- Produces:
  - `def execute_command(cmd: ControlCommand, manager, flags, *, exchange: str) -> str`——按 `cmd.type` 派发，返回结果摘要字符串；执行失败抛异常（由调用方 Task 7 捕获写 FAILED）。
    - `CLOSE_GRID`：`payload={"grid_id","symbol","reason"}` → `manager.executor.close(grid_id, symbol, reason)`，返回 `'closed <grid_id>'`。
    - `OPEN_GRID`：`payload={"symbol","params","tag","offset","cap"?}`；若 `flags.get('trading_halted')` 为真，抛 `RuntimeError('halted')`；否则 `manager.executor.open(exchange, symbol, params, offset=offset, tag=tag, cap=cap)`，返回 `'opened <symbol> -> <grid_id>'`。
    - `PANIC_CLOSE_ALL`：遍历 `manager.executor.grids.list_active()` 中状态属 `ACTIVE_STATES` 的网格逐个 `close`；per-grid 隔离（单网格失败记入摘要不中断）；返回 `'panic closed N ok, M failed: ...'`。

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_execute_command.py
import json
import pytest
from gridtrade.runtime.commands import execute_command
from gridtrade.state.models import ControlCommand


class _Grid:
    def __init__(self, gid, symbol): self.id = gid; self.symbol = symbol; self.status = 'ACTIVE'


class _Grids:
    def __init__(self, grids): self._g = grids
    def list_active(self): return self._g


class _Executor:
    def __init__(self, grids=()):
        self.grids = _Grids(list(grids))
        self.closed = []; self.opened = []
        self.fail_on = set()
    def close(self, gid, symbol, reason):
        if gid in self.fail_on: raise RuntimeError('boom %s' % gid)
        self.closed.append((gid, symbol, reason))
    def open(self, exchange, symbol, params, *, offset=0, tag='', cap=None):
        self.opened.append((symbol, tag, cap)); return 'newgrid'


class _Manager:
    def __init__(self, executor): self.executor = executor


class _Flags:
    def __init__(self, halted=False): self._h = halted
    def get(self, name): return self._h if name == 'trading_halted' else False


def test_close_grid_calls_executor_close():
    ex = _Executor()
    cmd = ControlCommand(id='c1', type='CLOSE_GRID',
                         payload=json.dumps({'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT', 'reason': 'manual'}))
    out = execute_command(cmd, _Manager(ex), _Flags(), exchange='hyperliquid')
    assert ex.closed == [('g1', 'BTC/USDT:USDT', 'manual')]
    assert 'g1' in out


def test_open_grid_refused_when_halted():
    ex = _Executor()
    cmd = ControlCommand(id='c2', type='OPEN_GRID',
                         payload=json.dumps({'symbol': 'BTC/USDT:USDT', 'params': {}, 'tag': 'gt0', 'offset': 0}))
    with pytest.raises(RuntimeError):
        execute_command(cmd, _Manager(ex), _Flags(halted=True), exchange='hyperliquid')
    assert ex.opened == []


def test_open_grid_passes_cap_override():
    ex = _Executor()
    cmd = ControlCommand(id='c3', type='OPEN_GRID',
                         payload=json.dumps({'symbol': 'ETH/USDT:USDT', 'params': {'low_price': 1},
                                             'tag': 'gt0', 'offset': 0, 'cap': 250.0}))
    out = execute_command(cmd, _Manager(ex), _Flags(), exchange='hyperliquid')
    assert ex.opened == [('ETH/USDT:USDT', 'gt0', 250.0)]
    assert 'ETH/USDT:USDT' in out


def test_panic_closes_all_with_isolation():
    ex = _Executor([_Grid('g1', 'BTC/USDT:USDT'), _Grid('g2', 'ETH/USDT:USDT')])
    ex.fail_on = {'g2'}
    cmd = ControlCommand(id='c4', type='PANIC_CLOSE_ALL', payload='{"reason": "panic"}')
    out = execute_command(cmd, _Manager(ex), _Flags(), exchange='hyperliquid')
    assert ('g1', 'BTC/USDT:USDT', 'panic') in ex.closed     # 健康网格照平
    assert 'failed' in out and 'g2' in out                    # 坏网格记入摘要、不中断
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_execute_command.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.runtime.commands`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/runtime/commands.py
"""控制指令执行分发：CLOSE_GRID / OPEN_GRID / PANIC_CLOSE_ALL。只在 monitor 调用。"""
import json

from gridtrade.state.models import ACTIVE_STATES


def execute_command(cmd, manager, flags, *, exchange: str) -> str:
    ex = manager.executor
    p = json.loads(cmd.payload or '{}')
    if cmd.type == 'CLOSE_GRID':
        ex.close(p['grid_id'], p['symbol'], p.get('reason', 'manual'))
        return 'closed %s' % p['grid_id']
    if cmd.type == 'OPEN_GRID':
        if flags.get('trading_halted'):
            raise RuntimeError('trading halted: OPEN refused')
        gid = ex.open(exchange, p['symbol'], p['params'],
                      offset=int(p.get('offset', 0)), tag=p.get('tag', ''),
                      cap=p.get('cap'))
        return 'opened %s -> %s' % (p['symbol'], gid)
    if cmd.type == 'PANIC_CLOSE_ALL':
        active = [g for g in ex.grids.list_active() if g.status in ACTIVE_STATES]
        ok, failed = [], []
        for g in active:
            try:
                ex.close(g.id, g.symbol, 'panic')
                ok.append(g.id)
            except Exception as exc:                 # per-grid 隔离，不中断其他
                failed.append('%s:%r' % (g.id, exc))
        msg = 'panic closed %d ok' % len(ok)
        if failed:
            msg += ', %d failed: %s' % (len(failed), '; '.join(failed))
        return msg
    raise ValueError('unknown command type: %s' % cmd.type)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_execute_command.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/runtime/commands.py tests/runtime/test_execute_command.py
git commit -m "feat(control): execute_command 分发（CLOSE/OPEN halt 门控/PANIC per-grid 隔离）"
```

---

### Task 7: consume_one（认领→执行→终态→审计）

**Files:**
- Modify: `gridtrade/runtime/commands.py`
- Test: `tests/runtime/test_consume_one.py`

**Interfaces:**
- Consumes: `CommandRepository`（`claim_next`/`finish`）、`AuditRepository`（`add`）、`execute_command`（Task 6）、`CMD_DONE`/`CMD_FAILED`。
- Produces（追加到 `commands.py`）：
  - `def consume_one(commands, audit, manager, flags, *, exchange: str) -> Optional[str]`——`claim_next()` 取一条；无则返回 None；有则 `execute_command`，成功 `finish(DONE, result)` + `audit.add(actor=cmd.created_by, action='CMD_RESULT', target=cmd.id, detail=result, outcome='ok')`，失败 `finish(FAILED, repr(exc))` + 审计 `outcome='fail'`；返回该 cmd.id。

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_consume_one.py
import json
from gridtrade.runtime.commands import consume_one
from gridtrade.state.control import CommandRepository, AuditRepository
from gridtrade.state.models import CMD_DONE, CMD_FAILED


class _Grids:
    def list_active(self): return []
class _Executor:
    def __init__(self): self.grids = _Grids(); self.closed = []
    def close(self, gid, symbol, reason): self.closed.append(gid)
class _Manager:
    def __init__(self): self.executor = _Executor()
class _Flags:
    def get(self, name): return False


def test_consume_one_success_marks_done_and_audits(store):
    cmds = CommandRepository(store); audit = AuditRepository(store)
    c = cmds.enqueue('CLOSE_GRID', json.dumps({'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT'}),
                     created_by='admin')
    cid = consume_one(cmds, audit, _Manager(), _Flags(), exchange='hyperliquid')
    assert cid == c.id
    assert cmds.get(c.id).status == CMD_DONE
    assert any(a.action == 'CMD_RESULT' and a.outcome == 'ok' for a in audit.list_recent())


def test_consume_one_failure_marks_failed(store):
    cmds = CommandRepository(store); audit = AuditRepository(store)
    c = cmds.enqueue('OPEN_GRID', json.dumps({'symbol': 'X', 'params': {}, 'tag': 't'}),
                     created_by='admin')

    class _HaltFlags:
        def get(self, name): return name == 'trading_halted'
    consume_one(cmds, audit, _Manager(), _HaltFlags(), exchange='hyperliquid')
    got = cmds.get(c.id)
    assert got.status == CMD_FAILED and 'halted' in (got.result or '').lower()
    assert any(a.outcome == 'fail' for a in audit.list_recent())


def test_consume_one_returns_none_when_empty(store):
    assert consume_one(CommandRepository(store), AuditRepository(store),
                       _Manager(), _Flags(), exchange='hyperliquid') is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_consume_one.py -v`
Expected: FAIL — `ImportError: cannot import name 'consume_one'`

- [ ] **Step 3: Write minimal implementation** (append to `gridtrade/runtime/commands.py`)

```python
# --- 追加到 gridtrade/runtime/commands.py ---
from typing import Optional

from gridtrade.state.models import CMD_DONE, CMD_FAILED


def consume_one(commands, audit, manager, flags, *, exchange: str) -> Optional[str]:
    cmd = commands.claim_next()
    if cmd is None:
        return None
    try:
        result = execute_command(cmd, manager, flags, exchange=exchange)
        commands.finish(cmd.id, CMD_DONE, result)
        audit.add(cmd.created_by or 'system', 'CMD_RESULT', cmd.id,
                  detail=result, outcome='ok')
    except Exception as exc:
        commands.finish(cmd.id, CMD_FAILED, repr(exc))
        audit.add(cmd.created_by or 'system', 'CMD_RESULT', cmd.id,
                  detail=repr(exc), outcome='fail')
    return cmd.id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_consume_one.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/runtime/commands.py tests/runtime/test_consume_one.py
git commit -m "feat(control): consume_one（认领→执行→DONE/FAILED→审计）"
```

---

### Task 8: factory 接线控制仓储 + monitor 集成（halt 门控 + 消费指令）

**Files:**
- Modify: `gridtrade/runtime/factory.py`（Runtime bundle 加 `flags`/`commands`/`audit`）
- Modify: `gridtrade/runtime/cycles.py`（`run_monitor_cycle` 加 halt 门控 + 消费一条指令）
- Modify: `gridtrade/runtime/monitor.py`（`run_monitor` 把控制仓储传入 cycle）
- Test: `tests/runtime/test_monitor_cycle_control.py`

**Interfaces:**
- Consumes: `ControlFlagRepository`/`CommandRepository`/`AuditRepository`（Task 2-4）、`consume_one`（Task 7）。`manager.monitor_all()` 现有签名不变。
- Produces:
  - `factory.Runtime` 增字段 `flags`, `commands`, `audit`；`build_runtime` 构造它们（同一 store）。
  - `run_monitor_cycle(reconciler, manager, log=print, *, flags=None, commands=None, audit=None, exchange='')`——新增可选参数；当三者齐备时：循环末尾调 `consume_one(...)` 消费一条指令；并在调用 `manager.monitor_all()` 前读 `flags.get('trading_halted')`，为真则跳过补单/开仓。**门控实现见下：** `manager.monitor_all(halt=...)` 需要 manager 支持——本任务改为更小侵入：`run_monitor_cycle` 在 halt 时传 `manager.monitor_all(skip_replenish=True)`。若 `monitor_all` 暂不支持该参数，则本任务同时给 `GridManager.monitor_all` 加 `skip_replenish=False` 形参（默认不变），halt 时只跑止损/记账、跳过补单。
- 默认参数全部可选 → 现有 monitor 调用与既有测试不受影响。

> **实现注意（halt 门控落点）：** 读 `gridtrade/execution/manager.py` 的 `monitor_all` 实现，确认补单调用点；加一个 `skip_replenish=False` 形参，在补单分支前 `if skip_replenish: continue`（止损/止盈/记账保持）。若 `monitor_all` 结构不便加参，退而在 `run_monitor_cycle` 层：halt 时不调用 `monitor_all` 的补单路径——以代码实际结构为准，但**必须保证 halt 下止损仍触发、补单被跳过**，并在测试中验证。

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_monitor_cycle_control.py
import json
from gridtrade.runtime.cycles import run_monitor_cycle
from gridtrade.state.control import CommandRepository, AuditRepository, ControlFlagRepository
from gridtrade.state.models import CMD_DONE


class _Grids:
    def list_active(self): return []
class _Executor:
    def __init__(self): self.grids = _Grids(); self.closed = []
    def is_loaded(self, gid): return True
    def close(self, gid, symbol, reason): self.closed.append(gid)
class _Manager:
    def __init__(self): self.executor = _Executor()
    def monitor_all(self, skip_replenish=False):
        self.last_skip = skip_replenish; return []
class _Reconciler:
    def __init__(self, ex): self.ex = ex


def test_monitor_cycle_consumes_one_command(store):
    cmds = CommandRepository(store); audit = AuditRepository(store)
    flags = ControlFlagRepository(store)
    cmds.enqueue('CLOSE_GRID', json.dumps({'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT'}),
                 created_by='admin')
    ex = _Executor(); mgr = _Manager(); mgr.executor = ex
    run_monitor_cycle(_Reconciler(ex), mgr, flags=flags, commands=cmds, audit=audit,
                      exchange='hyperliquid')
    assert ex.closed == ['g1']                          # 指令被消费执行
    assert cmds.list_recent()[0].status == CMD_DONE


def test_monitor_cycle_halt_skips_replenish(store):
    flags = ControlFlagRepository(store); flags.set('trading_halted', True, actor='admin')
    ex = _Executor(); mgr = _Manager(); mgr.executor = ex
    run_monitor_cycle(_Reconciler(ex), mgr, flags=flags,
                      commands=CommandRepository(store), audit=AuditRepository(store),
                      exchange='hyperliquid')
    assert mgr.last_skip is True                         # halt → monitor_all(skip_replenish=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_monitor_cycle_control.py -v`
Expected: FAIL — `TypeError: run_monitor_cycle() got an unexpected keyword argument 'flags'`

- [ ] **Step 3: Write minimal implementation**

(a) `gridtrade/execution/manager.py`：给 `monitor_all` 加 `skip_replenish=False` 形参，在补单调用处用它门控（止损/记账不变）。

(b) `gridtrade/runtime/cycles.py`：`run_monitor_cycle` 签名加 `*, flags=None, commands=None, audit=None, exchange=''`；把 `monitored = manager.monitor_all()` 改为：

```python
    halted = bool(flags.get('trading_halted')) if flags is not None else False
    monitored = manager.monitor_all(skip_replenish=halted)
```

并在 `return` 之前消费一条指令：

```python
    if commands is not None and audit is not None:
        from gridtrade.runtime.commands import consume_one
        consume_one(commands, audit, manager, flags, exchange=exchange)
```

(c) `gridtrade/runtime/factory.py`：`Runtime` dataclass 加 `flags`, `commands`, `audit` 字段；`build_runtime` 末尾：

```python
    from gridtrade.state.control import (ControlFlagRepository, CommandRepository,
                                        AuditRepository)
    return Runtime(
        config=config, adapter=adapter, store=store, executor=executor,
        manager=manager, trigger_engine=trigger_engine,
        reconciler=Reconciler(executor),
        heartbeats=HeartbeatRepository(store), event_bus=bus,
        flags=ControlFlagRepository(store), commands=CommandRepository(store),
        audit=AuditRepository(store),
    )
```

(d) `gridtrade/runtime/monitor.py`：`run_monitor` 里 `cycle_fn(rt.reconciler, rt.manager)` 改为传控制仓储：

```python
        cycle_fn(rt.reconciler, rt.manager, flags=rt.flags, commands=rt.commands,
                 audit=rt.audit, exchange=rt.config.exchange)
```

（`cycle_fn` 默认仍是 `run_monitor_cycle`；既有不带控制参的测试因参数可选不受影响。）

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_monitor_cycle_control.py -v`
然后回归：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime tests/execution -q`
Expected: PASS（新测 2 passed；runtime/execution 既有测试不回归）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/runtime/factory.py gridtrade/runtime/cycles.py gridtrade/runtime/monitor.py gridtrade/execution/manager.py tests/runtime/test_monitor_cycle_control.py
git commit -m "feat(control): monitor 集成（halt 门控补单 + 每周期消费一条指令 + factory 接线）"
```

---

### Task 9: scheduler halt/paused 门控

**Files:**
- Modify: `gridtrade/runtime/scheduler.py`（`run_scheduler_once` 开头读标志）
- Test: `tests/runtime/test_scheduler_control.py`

**Interfaces:**
- Consumes: `runtime.flags`（Task 8 起 Runtime 带 `flags`）。
- Produces: `run_scheduler_once` 在选币前读 `runtime.flags.get('trading_halted')` 或 `get('scheduler_paused')`，任一为真则跳过本轮（记日志，beat 心跳照常），返回 `{'skipped': 'halted'|'paused'}`。

- [ ] **Step 1: Write the failing test**

```python
# tests/runtime/test_scheduler_control.py
from gridtrade.runtime.scheduler import run_scheduler_once


class _Flags:
    def __init__(self, halted=False, paused=False): self._h = halted; self._p = paused
    def get(self, name):
        return {'trading_halted': self._h, 'scheduler_paused': self._p}.get(name, False)


class _HB:
    def __init__(self): self.beats = []
    def beat(self, m): self.beats.append(m)


class _RT:
    def __init__(self, flags):
        self.flags = flags; self.heartbeats = _HB()
        self.config = type('C', (), {'exchange': 'fake'})()


def test_scheduler_skips_when_paused():
    rt = _RT(_Flags(paused=True))
    out = run_scheduler_once(rt, now_fn=lambda: 0.0)
    assert out.get('skipped') == 'paused'
    assert rt.heartbeats.beats == ['scheduler']         # 心跳照常


def test_scheduler_skips_when_halted():
    rt = _RT(_Flags(halted=True))
    out = run_scheduler_once(rt, now_fn=lambda: 0.0)
    assert out.get('skipped') == 'halted'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_scheduler_control.py -v`
Expected: FAIL（当前 `run_scheduler_once` 会去取 `rt.config.scheduler_period` 等 → AttributeError，而非 `skipped`）

- [ ] **Step 3: Write minimal implementation**

在 `gridtrade/runtime/scheduler.py` 的 `run_scheduler_once` 开头（取 `run_time` 之前）插入门控：

```python
def run_scheduler_once(runtime, *, now_fn=time.time,
                       fetch_candles=fetch_universe_candles) -> dict:
    rt = runtime
    flags = getattr(rt, 'flags', None)
    if flags is not None:
        if flags.get('trading_halted'):
            rt.heartbeats.beat('scheduler')
            return {'skipped': 'halted'}
        if flags.get('scheduler_paused'):
            rt.heartbeats.beat('scheduler')
            return {'skipped': 'paused'}
    run_time = pd.Timestamp(now_fn(), unit='s').floor('H')
    # ... 其余原逻辑不变 ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_scheduler_control.py -v`
然后回归：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime -q`
Expected: PASS（2 passed；既有 scheduler 测试不回归——`getattr(rt,'flags',None)` 容旧 runtime）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/runtime/scheduler.py tests/runtime/test_scheduler_control.py
git commit -m "feat(control): scheduler halt/paused 门控（跳过选币、心跳照常）"
```

---

### Task 10: 候选币池 / 开仓默认参数计算（control_compute.py）

**Files:**
- Create: `gridtrade/dashboard/control_compute.py`
- Test: `tests/dashboard/test_control_compute.py`

**Interfaces:**
- Consumes: `runtime.trigger_engine.collect(ctx)`（返回 `List[GridProposal]`，每个含 `.symbol`、`.grid_params`(dict)、`.tag`、`.offset`）；`gridtrade.execution.triggers.TriggerContext`；`gridtrade.runtime.universe.resolve_live_universe`；`gridtrade.runtime.scheduler.fetch_universe_candles`；`gridtrade.config.DEFAULT_STRATEGY_CONFIG`。
- Produces:
  - `def compute_proposals(runtime, *, now_fn=time.time, fetch_candles=None) -> List[dict]`——复刻 `run_scheduler_once` 的「取 universe→拉 K 线→建 ctx→collect」但**不开仓**；返回 `[{'symbol','grid_params','tag','offset'}, ...]`（把 `GridProposal` 摊平成 dict 供模板/表单用）。
  - `def defaults_for_symbol(runtime, symbol: str, *, now_fn=time.time, fetch_candles=None) -> Optional[dict]`——调用 `compute_proposals` 后按 symbol 过滤，命中返回该 dict，否则 None。

> 说明：`compute_proposals` 是只读——只 `fetch_candles`（行情）+ 纯 core 计算，不写库、不下单。慢（拉全币池 K 线）由调用方（web 路由）显示 loading；本期同步跑。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_control_compute.py
from gridtrade.dashboard.control_compute import compute_proposals, defaults_for_symbol


class _Proposal:
    def __init__(self, symbol, gp): self.symbol = symbol; self.grid_params = gp
    tag = 'gt0'; offset = 0


class _TriggerEngine:
    def __init__(self, props): self._p = props
    def collect(self, ctx): return self._p


class _RT:
    def __init__(self, props):
        self.trigger_engine = _TriggerEngine(props)
        self.adapter = object()
        self.config = type('C', (), {'exchange': 'fake', 'blacklist': (), 'whitelist': (),
                                     'utc_offset': 8, 'scheduler_period': '12H'})()


def _fake_fetch(adapter, universe, run_time, **kw): return {}


def test_compute_proposals_flattens(monkeypatch):
    import gridtrade.dashboard.control_compute as m
    monkeypatch.setattr(m, 'resolve_live_universe', lambda *a, **k: ['BTC/USDT:USDT'])
    rt = _RT([_Proposal('BTC/USDT:USDT', {'low_price': 90.0, 'high_price': 110.0,
                                          'grid_count': 10, 'stop_low_price': 80.0,
                                          'stop_high_price': 120.0})])
    out = compute_proposals(rt, now_fn=lambda: 0.0, fetch_candles=_fake_fetch)
    assert out[0]['symbol'] == 'BTC/USDT:USDT'
    assert out[0]['grid_params']['grid_count'] == 10
    assert out[0]['tag'] == 'gt0'


def test_defaults_for_symbol_filters(monkeypatch):
    import gridtrade.dashboard.control_compute as m
    monkeypatch.setattr(m, 'resolve_live_universe', lambda *a, **k: ['BTC/USDT:USDT', 'ETH/USDT:USDT'])
    rt = _RT([_Proposal('BTC/USDT:USDT', {'low_price': 1}), _Proposal('ETH/USDT:USDT', {'low_price': 2})])
    d = defaults_for_symbol(rt, 'ETH/USDT:USDT', now_fn=lambda: 0.0, fetch_candles=_fake_fetch)
    assert d['symbol'] == 'ETH/USDT:USDT' and d['grid_params']['low_price'] == 2
    assert defaults_for_symbol(rt, 'NOPE', now_fn=lambda: 0.0, fetch_candles=_fake_fetch) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_control_compute.py -v`
Expected: FAIL — `ModuleNotFoundError: gridtrade.dashboard.control_compute`

- [ ] **Step 3: Write minimal implementation**

```python
# gridtrade/dashboard/control_compute.py
"""候选币池排名 + 单币开仓默认参数：复用 trigger_engine 的金标选币管线，只读不下单。"""
import time

import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG
from gridtrade.execution.triggers import TriggerContext
from gridtrade.runtime.scheduler import fetch_universe_candles
from gridtrade.runtime.universe import resolve_live_universe


def compute_proposals(runtime, *, now_fn=time.time, fetch_candles=None):
    rt = runtime
    fetch = fetch_candles or fetch_universe_candles
    run_time = pd.Timestamp(now_fn(), unit='s').floor('H')
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist, rt.config.whitelist)
    candles = fetch(rt.adapter, universe, run_time,
                    max_candle_num=DEFAULT_STRATEGY_CONFIG['max_candle_num'])
    ctx = TriggerContext(rt.config.exchange, run_time, candles)
    out = []
    for p in rt.trigger_engine.collect(ctx):
        out.append({'symbol': p.symbol, 'grid_params': dict(p.grid_params),
                    'tag': getattr(p, 'tag', ''), 'offset': getattr(p, 'offset', 0)})
    return out


def defaults_for_symbol(runtime, symbol, *, now_fn=time.time, fetch_candles=None):
    for p in compute_proposals(runtime, now_fn=now_fn, fetch_candles=fetch_candles):
        if p['symbol'] == symbol:
            return p
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_control_compute.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/control_compute.py tests/dashboard/test_control_compute.py
git commit -m "feat(control): control_compute（候选币池排名 + 单币默认参数，复用选币管线只读）"
```

---

### Task 11: web 标志路由（halt / panic / scheduler 暂停）+ 审计

**Files:**
- Modify: `gridtrade/dashboard/app.py`（`create_app` 增 `flags`/`commands`/`audit` 形参 + 标志路由）
- Modify: `gridtrade/runtime/web.py`（`build_web_app` 把 `rt.flags/commands/audit` 传入 `create_app`）
- Test: `tests/dashboard/test_app_control_flags.py`

**Interfaces:**
- Consumes: `ControlFlagRepository`/`CommandRepository`/`AuditRepository`（实例由 `build_web_app` 注入）；P1 `_user(request)`；`CommandRepository.enqueue`。
- Produces:
  - `create_app(store, adapter, *, username, password_hash, session_secret, throttle=None, stale_threshold_sec=30.0, flags=None, commands=None, audit=None)`——新增三可选仓储参（None 时用 `store` 自建，保持 P1 测试可用）。
  - 路由：
    - `POST /control/scheduler` body `action=pause|resume` → `flags.set('scheduler_paused', action=='pause', actor=user)` + 审计 `FLAG_SET`；302 回 `/controls`。
    - `POST /control/halt` body `action=on|off` → `flags.set('trading_halted', action=='on', actor=user)` + 审计；302。
    - `POST /control/panic` body `confirm=PANIC` → 校验 confirm；置 `trading_halted=true` + `commands.enqueue('PANIC_CLOSE_ALL', '{"reason":"panic"}', created_by=user)` + 审计 `CMD_SUBMIT`；302。未登录一律 302 /login。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_app_control_flags.py
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.control import ControlFlagRepository, CommandRepository, AuditRepository
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _client(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000),
                     session_secret='sek',
                     flags=ControlFlagRepository(store),
                     commands=CommandRepository(store),
                     audit=AuditRepository(store))
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def test_halt_sets_flag_and_audits(store):
    c = _client(store)
    r = c.post('/control/halt', data={'action': 'on'}, follow_redirects=False)
    assert r.status_code == 302
    assert ControlFlagRepository(store).get('trading_halted') is True
    assert any(a.action == 'FLAG_SET' for a in AuditRepository(store).list_recent())


def test_panic_requires_confirm_and_enqueues(store):
    c = _client(store)
    bad = c.post('/control/panic', data={'confirm': 'nope'}, follow_redirects=False)
    assert CommandRepository(store).list_recent() == []          # 确认词不对 → 不入队
    ok = c.post('/control/panic', data={'confirm': 'PANIC'}, follow_redirects=False)
    assert ok.status_code == 302
    assert ControlFlagRepository(store).get('trading_halted') is True
    cmds = CommandRepository(store).list_recent()
    assert len(cmds) == 1 and cmds[0].type == 'PANIC_CLOSE_ALL'


def test_control_routes_require_login(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek',
                     flags=ControlFlagRepository(store), commands=CommandRepository(store),
                     audit=AuditRepository(store))
    anon = TestClient(app, base_url='https://testserver')
    r = anon.post('/control/halt', data={'action': 'on'}, follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'].endswith('/login')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_control_flags.py -v`
Expected: FAIL — `create_app() got an unexpected keyword argument 'flags'`

- [ ] **Step 3: Write minimal implementation**

(a) `app.py`：`create_app` 签名加 `flags=None, commands=None, audit=None`；函数体开头：

```python
    from gridtrade.state.control import (ControlFlagRepository, CommandRepository,
                                        AuditRepository)
    flags = flags or ControlFlagRepository(store)
    commands = commands or CommandRepository(store)
    audit = audit or AuditRepository(store)
```

加路由（放在已有路由后、`return app` 前）：

```python
    @app.post('/control/scheduler')
    def control_scheduler(request: Request, action: str = Form(...)):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        paused = action == 'pause'
        flags.set('scheduler_paused', paused, actor=u)
        audit.add(u, 'FLAG_SET', 'scheduler_paused',
                  detail='{"value": %s}' % ('true' if paused else 'false'))
        return RedirectResponse('/controls', status_code=302)

    @app.post('/control/halt')
    def control_halt(request: Request, action: str = Form(...)):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        on = action == 'on'
        flags.set('trading_halted', on, actor=u)
        audit.add(u, 'FLAG_SET', 'trading_halted',
                  detail='{"value": %s}' % ('true' if on else 'false'))
        return RedirectResponse('/controls', status_code=302)

    @app.post('/control/panic')
    def control_panic(request: Request, confirm: str = Form('')):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        if confirm != 'PANIC':
            return RedirectResponse('/controls?err=confirm', status_code=302)
        flags.set('trading_halted', True, actor=u)
        cmd = commands.enqueue('PANIC_CLOSE_ALL', '{"reason": "panic"}', created_by=u)
        audit.add(u, 'CMD_SUBMIT', cmd.id, detail='{"type": "PANIC_CLOSE_ALL"}')
        return RedirectResponse('/controls', status_code=302)
```

(b) `gridtrade/runtime/web.py`：`build_web_app` 里 `create_app(...)` 调用加：

```python
        flags=rt.flags, commands=rt.commands, audit=rt.audit,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_control_flags.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/app.py gridtrade/runtime/web.py tests/dashboard/test_app_control_flags.py
git commit -m "feat(control): web 标志路由（halt/panic/scheduler）+ 审计 + 注入控制仓储"
```

---

### Task 12: web 指令路由（关网格 / 开网格）

**Files:**
- Modify: `gridtrade/dashboard/app.py`（关/开网格路由 + 开仓表单 GET）
- Test: `tests/dashboard/test_app_control_commands.py`

**Interfaces:**
- Consumes: `commands.enqueue`、`audit.add`、`control_compute.defaults_for_symbol`、`build_grid_detail`（校验网格存在，P1 queries）、`_user`。需要 `runtime` 用于 compute——在 `create_app` 增可选 `compute_fn=None` 注入点（默认用真实 `control_compute.defaults_for_symbol(adapter+config 包装)`；测试注入桩）。
- Produces:
  - `POST /control/close` body `grid_id, symbol, reason` → 入队 `CLOSE_GRID` + 审计；302 回来源页。
  - `GET /open?symbol=...` → 渲染开仓表单，预填 `defaults_for_symbol`（无 symbol 时只列候选）。
  - `POST /open` body `symbol, low_price, high_price, grid_count, stop_low_price, stop_high_price, cap?, tag?, offset?` → 组 `params` dict 入队 `OPEN_GRID` + 审计；302 回 `/controls`。
- `create_app` 增 `compute_fn=None`（签名 `compute_fn(symbol) -> Optional[dict]`），默认包装 `control_compute`；测试注入。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_app_control_commands.py
import json
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.control import ControlFlagRepository, CommandRepository, AuditRepository
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _client(store, compute_fn=None):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek',
                     flags=ControlFlagRepository(store), commands=CommandRepository(store),
                     audit=AuditRepository(store), compute_fn=compute_fn)
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def test_close_enqueues_close_grid(store):
    c = _client(store)
    r = c.post('/control/close',
               data={'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT', 'reason': 'manual'},
               follow_redirects=False)
    assert r.status_code == 302
    cmds = CommandRepository(store).list_recent()
    assert len(cmds) == 1 and cmds[0].type == 'CLOSE_GRID'
    assert json.loads(cmds[0].payload)['grid_id'] == 'g1'


def test_open_form_prefills_defaults(store):
    c = _client(store, compute_fn=lambda symbol: {
        'symbol': symbol, 'tag': 'gt0', 'offset': 0,
        'grid_params': {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 10,
                        'stop_low_price': 80.0, 'stop_high_price': 120.0}})
    r = c.get('/open?symbol=BTC/USDT:USDT')
    assert r.status_code == 200
    assert '110' in r.text and 'BTC/USDT:USDT' in r.text


def test_open_post_enqueues_open_grid_with_overridden_cap(store):
    c = _client(store)
    r = c.post('/open', data={'symbol': 'ETH/USDT:USDT', 'low_price': '1', 'high_price': '2',
                              'grid_count': '8', 'stop_low_price': '0.8', 'stop_high_price': '2.2',
                              'cap': '250', 'tag': 'gt0', 'offset': '0'},
               follow_redirects=False)
    assert r.status_code == 302
    cmd = CommandRepository(store).list_recent()[0]
    assert cmd.type == 'OPEN_GRID'
    p = json.loads(cmd.payload)
    assert p['symbol'] == 'ETH/USDT:USDT' and p['cap'] == 250.0
    assert p['params']['grid_count'] == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_control_commands.py -v`
Expected: FAIL — `create_app() got an unexpected keyword argument 'compute_fn'`

- [ ] **Step 3: Write minimal implementation**

`app.py`：`create_app` 加 `compute_fn=None`；若 None 则包装真实计算（实现期：`from gridtrade.dashboard import control_compute` + 用闭包持 `runtime`——但 `create_app` 当前只有 store/adapter，没有完整 runtime。**落点：** 由 `build_web_app` 构造 `compute_fn = lambda sym: control_compute.defaults_for_symbol(rt, sym)` 注入 `create_app`；`create_app` 内 `compute_fn` 为 None 时开仓表单不预填（只渲染空表单），保证单元测试不依赖行情）。加路由：

```python
    @app.post('/control/close')
    def control_close(request: Request, grid_id: str = Form(...),
                      symbol: str = Form(...), reason: str = Form('manual')):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        payload = '{"grid_id": %s, "symbol": %s, "reason": %s}' % (
            _json(grid_id), _json(symbol), _json(reason))
        cmd = commands.enqueue('CLOSE_GRID', payload, created_by=u)
        audit.add(u, 'CMD_SUBMIT', cmd.id, detail='{"type": "CLOSE_GRID"}')
        return RedirectResponse('/controls', status_code=302)

    @app.get('/open', response_class=HTMLResponse)
    def open_form(request: Request, symbol: str = ''):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        prefill = compute_fn(symbol) if (symbol and compute_fn) else None
        return templates.TemplateResponse(request, 'open.html',
                                          {'symbol': symbol, 'prefill': prefill})

    @app.post('/open')
    def open_submit(request: Request, symbol: str = Form(...),
                    low_price: float = Form(...), high_price: float = Form(...),
                    grid_count: int = Form(...), stop_low_price: float = Form(...),
                    stop_high_price: float = Form(...), cap: str = Form(''),
                    tag: str = Form('gt0'), offset: int = Form(0)):
        u = _user(request)
        if not u:
            return RedirectResponse('/login', status_code=302)
        import json as _j
        params = {'low_price': low_price, 'high_price': high_price,
                  'grid_count': grid_count, 'stop_low_price': stop_low_price,
                  'stop_high_price': stop_high_price}
        body = {'symbol': symbol, 'params': params, 'tag': tag, 'offset': offset}
        if cap.strip():
            body['cap'] = float(cap)
        cmd = commands.enqueue('OPEN_GRID', _j.dumps(body), created_by=u)
        audit.add(u, 'CMD_SUBMIT', cmd.id, detail='{"type": "OPEN_GRID"}')
        return RedirectResponse('/controls', status_code=302)
```

`app.py` 顶部加小工具 `import json as _jsonlib` 并 `def _json(s): return _jsonlib.dumps(s)`（CLOSE 路由用，避免手拼字符串注入）。同时创建最小 `open.html`（Task 13 会完善，此处先放可渲染骨架）：

```html
<!-- gridtrade/dashboard/templates/open.html -->
{% extends "base.html" %}{% block content %}
<h1>Open Grid</h1>
<form method="post" action="/open" onsubmit="return confirm('确认开网格 ' + this.symbol.value + '?')">
  <label>symbol <input name="symbol" value="{{ symbol }}"></label>
  {% set gp = (prefill.grid_params if prefill else {}) %}
  <label>low_price <input name="low_price" value="{{ gp.low_price if gp else '' }}"></label>
  <label>high_price <input name="high_price" value="{{ gp.high_price if gp else '' }}"></label>
  <label>grid_count <input name="grid_count" value="{{ gp.grid_count if gp else '' }}"></label>
  <label>stop_low_price <input name="stop_low_price" value="{{ gp.stop_low_price if gp else '' }}"></label>
  <label>stop_high_price <input name="stop_high_price" value="{{ gp.stop_high_price if gp else '' }}"></label>
  <label>cap（留空=默认）<input name="cap" value=""></label>
  <input type="hidden" name="tag" value="{{ prefill.tag if prefill else 'gt0' }}">
  <input type="hidden" name="offset" value="{{ prefill.offset if prefill else 0 }}">
  <button type="submit">提交开仓</button>
</form>
{% endblock %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_control_commands.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/app.py gridtrade/dashboard/templates/open.html tests/dashboard/test_app_control_commands.py
git commit -m "feat(control): web 关/开网格指令路由 + 开仓表单预填"
```

---

### Task 13: Controls / Universe 页 + 关按钮 + 审计视图 + web.py 接 compute_fn

**Files:**
- Modify: `gridtrade/dashboard/app.py`（GET `/controls`、GET `/universe`）
- Create: `gridtrade/dashboard/templates/controls.html`, `gridtrade/dashboard/templates/universe.html`
- Modify: `gridtrade/dashboard/templates/overview.html`, `detail.html`（活跃网格加「关」按钮）
- Modify: `gridtrade/dashboard/templates/base.html`（导航加 Controls / Universe）
- Modify: `gridtrade/runtime/web.py`（注入 `compute_fn` + `universe_fn`）
- Test: `tests/dashboard/test_app_control_pages.py`

**Interfaces:**
- Consumes: `flags.get`、`commands.list_recent`、`audit.list_recent`、`compute_fn`（单币）、`universe_fn()`（候选列表，`create_app` 新增可选 `universe_fn=None`，返回 `List[dict]`）。
- Produces:
  - `GET /controls`：渲染 halt/scheduler 当前态 + 两档 kill 表单 + 指令队列(`commands.list_recent`) + 审计(`audit.list_recent`)。
  - `GET /universe`：调 `universe_fn()` 列候选币池排名，每行链到 `/open?symbol=`。
  - overview/detail 每个活跃网格行：一个内联 `POST /control/close` 小表单按钮（带 `confirm()`）。
  - `web.py`：`compute_fn = lambda s: control_compute.defaults_for_symbol(rt, s)`；`universe_fn = lambda: control_compute.compute_proposals(rt)`。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_app_control_pages.py
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.state.control import ControlFlagRepository, CommandRepository, AuditRepository
from gridtrade.exchanges.base import Balance


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def _client(store, universe_fn=None):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek',
                     flags=ControlFlagRepository(store), commands=CommandRepository(store),
                     audit=AuditRepository(store), universe_fn=universe_fn)
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    return c


def test_controls_page_shows_halt_state_and_audit(store):
    ControlFlagRepository(store).set('trading_halted', True, actor='admin')
    AuditRepository(store).add('admin', 'FLAG_SET', 'trading_halted', detail='{"value": true}')
    r = _client(store).get('/controls')
    assert r.status_code == 200
    assert 'trading_halted' in r.text or 'halt' in r.text.lower()


def test_universe_page_lists_candidates(store):
    c = _client(store, universe_fn=lambda: [{'symbol': 'BTC/USDT:USDT', 'tag': 'gt0',
                                            'offset': 0, 'grid_params': {'grid_count': 10}}])
    r = c.get('/universe')
    assert r.status_code == 200 and 'BTC/USDT:USDT' in r.text
    assert '/open?symbol=' in r.text


def test_pages_require_login(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek',
                     flags=ControlFlagRepository(store), commands=CommandRepository(store),
                     audit=AuditRepository(store))
    anon = TestClient(app, base_url='https://testserver')
    assert anon.get('/controls', follow_redirects=False).status_code == 302
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_control_pages.py -v`
Expected: FAIL — `create_app() got an unexpected keyword argument 'universe_fn'`（或路由 404）

- [ ] **Step 3: Write minimal implementation**

`app.py`：`create_app` 加 `universe_fn=None`；加两路由：

```python
    @app.get('/controls', response_class=HTMLResponse)
    def controls_page(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        return templates.TemplateResponse(request, 'controls.html', {
            'halted': flags.get('trading_halted'),
            'scheduler_paused': flags.get('scheduler_paused'),
            'commands': commands.list_recent(), 'audit': audit.list_recent()})

    @app.get('/universe', response_class=HTMLResponse)
    def universe_page(request: Request):
        if not _user(request):
            return RedirectResponse('/login', status_code=302)
        rows = universe_fn() if universe_fn else []
        return templates.TemplateResponse(request, 'universe.html', {'rows': rows})
```

`controls.html`：

```html
<!-- gridtrade/dashboard/templates/controls.html -->
{% extends "base.html" %}{% block content %}
<h1>Controls</h1>
<section class="ctl">
  <h2>Trading halt: <b class="{{ 'neg' if halted else 'pos' }}">{{ 'HALTED' if halted else 'live' }}</b></h2>
  <form method="post" action="/control/halt" onsubmit="return confirm('切换 trading halt?')">
    <input type="hidden" name="action" value="{{ 'off' if halted else 'on' }}">
    <button>{{ '恢复交易' if halted else '暂停交易 (halt)' }}</button>
  </form>
  <form method="post" action="/control/panic"
        onsubmit="return prompt('输入 PANIC 确认急平所有网格','')==='PANIC' ? (this.confirm.value='PANIC', true) : false">
    <input type="hidden" name="confirm" value="">
    <button class="danger">急平所有 (PANIC)</button>
  </form>
</section>
<section class="ctl">
  <h2>Scheduler: <b>{{ 'PAUSED' if scheduler_paused else 'active' }}</b></h2>
  <form method="post" action="/control/scheduler" onsubmit="return confirm('切换 scheduler?')">
    <input type="hidden" name="action" value="{{ 'resume' if scheduler_paused else 'pause' }}">
    <button>{{ '恢复 scheduler' if scheduler_paused else '暂停 scheduler' }}</button>
  </form>
</section>
<h2>Command queue</h2>
<table><thead><tr><th>type</th><th>status</th><th>by</th><th>result</th></tr></thead><tbody>
{% for c in commands %}<tr><td>{{ c.type }}</td><td>{{ c.status }}</td><td>{{ c.created_by }}</td><td>{{ c.result or '' }}</td></tr>{% endfor %}
</tbody></table>
<h2>Audit</h2>
<table><thead><tr><th>ts</th><th>actor</th><th>action</th><th>target</th><th>outcome</th></tr></thead><tbody>
{% for a in audit %}<tr><td>{{ a.ts | ms_to_human }}</td><td>{{ a.actor }}</td><td>{{ a.action }}</td><td>{{ a.target }}</td><td>{{ a.outcome }}</td></tr>{% endfor %}
</tbody></table>
{% endblock %}
```

`universe.html`：

```html
<!-- gridtrade/dashboard/templates/universe.html -->
{% extends "base.html" %}{% block content %}
<h1>候选币池</h1>
<table><thead><tr><th>symbol</th><th>tag</th><th>grid_count</th><th>开</th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.symbol }}</td><td>{{ r.tag }}</td>
<td>{{ r.grid_params.grid_count if r.grid_params else '' }}</td>
<td><a href="/open?symbol={{ r.symbol | urlencode }}">开网格</a></td></tr>{% endfor %}
</tbody></table>
{% if not rows %}<p>无候选（或未触发选币）</p>{% endif %}
{% endblock %}
```

overview.html 活跃网格行加关按钮（在 symbol 那格后追加一格）：

```html
  <td><form method="post" action="/control/close" onsubmit="return confirm('确认关网格 ' + this.symbol.value + '?')" style="display:inline">
    <input type="hidden" name="grid_id" value="{{ r.grid_id }}">
    <input type="hidden" name="symbol" value="{{ r.symbol }}">
    <input type="hidden" name="reason" value="manual">
    <button class="danger">关</button></form></td>
```

base.html 导航加链接：把 `<nav>` 改为
`<nav><a href="/">overview</a> <a href="/history">history</a> <a href="/controls">controls</a> <a href="/universe">universe</a></nav>`

`web.py`：`build_web_app` 注入：

```python
    from gridtrade.dashboard import control_compute
    return create_app(rt.store, rt.adapter, ...,
                      flags=rt.flags, commands=rt.commands, audit=rt.audit,
                      compute_fn=lambda s: control_compute.defaults_for_symbol(rt, s),
                      universe_fn=lambda: control_compute.compute_proposals(rt))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_app_control_pages.py -v`
然后全 dashboard 套件：`TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard -q`
Expected: PASS（新测 3 passed；P1 dashboard 测试不回归）

- [ ] **Step 5: Commit**

```bash
git add gridtrade/dashboard/app.py gridtrade/dashboard/templates gridtrade/runtime/web.py tests/dashboard/test_app_control_pages.py
git commit -m "feat(control): Controls/Universe 页 + 关按钮 + 审计视图 + web 接 compute/universe"
```

---

### Task 14: 手机响应式 CSS（含 P1 视图）

**Files:**
- Modify: `gridtrade/dashboard/static/app.css`（加 `@media` 响应式 + 触控尺寸 + danger 按钮样式）
- Modify: `gridtrade/dashboard/templates/base.html`（`<head>` 加 viewport meta；表格包一层 `.tablewrap`）
- Test: `tests/dashboard/test_responsive_assets.py`

**Interfaces:**
- 无代码接口。视觉项：窄屏（≤640px）下宽表格横向滚动 + 健康顶栏/导航 flex 换行 + 按钮 ≥44px 触控；桌面不变。
- 可断言项：viewport meta 存在；CSS 含 `@media` 与 `.danger`；登录后各页仍 200。

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_responsive_assets.py
from pathlib import Path
from starlette.testclient import TestClient
from gridtrade.dashboard.app import create_app
from gridtrade.dashboard.auth import hash_password
from gridtrade.exchanges.base import Balance

_DIR = Path(__file__).resolve().parents[2] / 'gridtrade' / 'dashboard'


class _Adapter:
    client = None
    def fetch_balance(self): return Balance(equity=1.0, cash=1.0)
    def fetch_price(self, s): return 100.0


def test_css_has_media_query_and_danger():
    css = (_DIR / 'static' / 'app.css').read_text()
    assert '@media' in css
    assert '.danger' in css


def test_base_has_viewport_meta(store):
    app = create_app(store, _Adapter(), username='admin',
                     password_hash=hash_password('pw', iterations=1000), session_secret='sek')
    c = TestClient(app, base_url='https://testserver')
    c.post('/login', data={'username': 'admin', 'password': 'pw'})
    html = c.get('/').text
    assert 'name="viewport"' in html and 'width=device-width' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_responsive_assets.py -v`
Expected: FAIL（`@media`/viewport 尚未加）

- [ ] **Step 3: Write minimal implementation**

`base.html` 的 `<head>` 加（在 charset 后）：

```html
  <meta name="viewport" content="width=device-width, initial-scale=1">
```

并把内容区表格的渲染交给 CSS 横向滚动——`base.html` 的 `<main>` 包裹不变，靠 CSS 处理（表格在窄屏可横向滚）。

`app.css` 追加：

```css
/* 触控 + 危险按钮 */
button{min-height:44px;padding:.4rem .8rem;cursor:pointer}
button.danger{background:#7f1d1d;border-color:#b91c1c;color:#fff}
.ctl{border:1px solid #333;padding:.6rem;margin:.6rem 0}
.ctl form{display:inline-block;margin-right:.6rem}

/* 窄屏（手机）：表格可横向滚动，顶栏/导航换行，字大一点 */
@media (max-width: 640px){
  body{margin:.5rem;font-size:1rem}
  table{display:block;overflow-x:auto;white-space:nowrap}
  .health{flex-direction:column;gap:.3rem;align-items:flex-start}
  .health nav{margin-left:0;display:flex;flex-wrap:wrap;gap:.6rem}
  th,td{font-size:.9rem}
  .login{margin:2rem auto;width:90%}
}
```

> 说明：用「表格横向滚动」而非堆叠卡片——更省改动、对所有现有表格（P1 四视图 + P2 队列/审计）一致生效，窄屏可滑动看全列。若后续要卡片式，再单独迭代。

- [ ] **Step 4: Run test to verify it passes**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard/test_responsive_assets.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run full dashboard + whole suite**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/dashboard -q && TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿（P1+P2 dashboard 测试 + 全套不回归）

- [ ] **Step 6: Commit**

```bash
git add gridtrade/dashboard/static/app.css gridtrade/dashboard/templates/base.html tests/dashboard/test_responsive_assets.py
git commit -m "feat(control): 手机响应式（viewport + @media 表格横滚 + 触控/danger 按钮）"
```

---

### Task 15: 文档更新（STATUS / DEPLOY / 设计指针）

**Files:**
- Modify: `docs/STATUS.md`（§5 web 进程行补 P2 控制能力；测试计数更新）
- Modify: `deploy/DEPLOY.md`（dashboard 段补「控制台动作经指令队列由 monitor 执行；halt/panic/scheduler 语义」）

**Interfaces:** 无代码；文档同步。

- [ ] **Step 1: 更新 STATUS.md §5 web 行**

把 web 进程描述补一句：

```markdown
  P2 控制台：kill 两档(halt/panic) + 关/开网格 + 暂停 scheduler，均经 control_commands 指令队列由 monitor 执行（web 零下单）；control_flags 标志门控；control_audit 审计。
```

- [ ] **Step 2: 更新 DEPLOY.md dashboard 段**

追加：

```markdown
### 控制台（P2）
- 控制动作 = web 写 DB（control_flags/control_commands/control_audit），monitor 每 ~5s 消费执行，web 永不下单。
- halt：冻结补单/开仓/选币，止损与记账照常。panic：置 halt + 入队全平（需输入 PANIC 确认）。
- 关/开网格、暂停 scheduler 同走指令/标志；审计与队列状态在 /controls 页可查。
```

- [ ] **Step 3: Commit**

```bash
git add docs/STATUS.md deploy/DEPLOY.md
git commit -m "docs(control): STATUS/DEPLOY 同步 P2 控制台（指令队列/halt/panic/审计）"
```

---

## Self-Review

**Spec 覆盖（逐节核对 2026-06-30-dashboard-p2-control-design.md）：**
- §3 架构（web 写意图 / monitor 执行 / 三表）→ T1–T4(表+仓储) / T6–T9(monitor·scheduler 执行) / T11–T13(web 写)。✅
- §4 数据模型三表 → T1。✅
- §5.1 halt 语义（冻加仓、止损照常）→ T8(monitor skip_replenish)、T9(scheduler 门控)、T6(OPEN halt 拒)。✅
- §5.2 panic（置 halt + 全平 + per-grid 隔离）→ T11(web 入队+置 halt)、T6(PANIC 隔离执行)。✅
- §5 动作表：暂停 scheduler→T9/T11；halt→T8/T11；panic→T6/T11；关网格→T6/T12；开网格→T5/T6/T10/T12；查币池→T10/T13。✅
- §6 UI（controls/open/universe 页 + 关按钮 + 确认 + 手机适配）→ T12/T13/T14。✅
- §7 鉴权/安全（登录后 POST、零下单、审计全覆盖、指令幂等）→ T11–T13(login gate)、T3(版本守卫)、T7(审计)。✅
- §8 测试（仓储/ monitor 消费/ scheduler 门控/ web 路由/ CSS）→ 各任务 TDD。✅
- §9 开放项（选币同步慢、每周期一条指令、panic 交错、断点微调）→ T10 注释、T8 设计、保留为实现注意。✅

**Placeholder 扫描：** 无 TBD/TODO；每个 code step 给完整代码 + 命令。T8 的 halt 门控落点给了明确「monitor_all 加 skip_replenish」方案与回退说明（非占位，是带约束的实现指引）。T12 的 `compute_fn` 注入点明确由 web.py 提供、单测注入桩。✅

**类型一致：** `ControlFlagRepository.get/set`、`CommandRepository.enqueue/claim_next/finish/list_recent`、`AuditRepository.add/list_recent` 在 T2–T4 定义，T7–T13 一致调用；`execute_command(cmd,manager,flags,*,exchange)`、`consume_one(commands,audit,manager,flags,*,exchange)` 在 T6/T7 定义，T8 一致调用；`create_app` 新增 `flags/commands/audit/compute_fn/universe_fn` 形参在 T11–T13 递增且默认 None（向后兼容 P1 测试）；`executor.open(...,cap=None)` 在 T5 定义、T6 使用；`run_monitor_cycle(...,*,flags,commands,audit,exchange)` 在 T8 定义、monitor.py 调用一致。✅
