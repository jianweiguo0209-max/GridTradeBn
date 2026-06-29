# 双模式 PG 测试 fixture + 真并发 TOCTOU 测试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 23 个用 `StateStore.in_memory()` 的测试文件改用一个双模式 `store` fixture（设 `TEST_DATABASE_URL` 走 Postgres、否则 SQLite），并新增 `transition_status` 的真线程并发 TOCTOU 测试。

**Architecture:** conftest 提供 `store`（双模式）与 `pg_store`（PG-only，无 PG 则 skip）两个 fixture + `_truncate_all` 隔离；各测试由内联建库改为消费 fixture；并发测试用真线程 + barrier 在真 PG 上验证版本守卫只放一个赢家。

**Tech Stack:** Python 3.9 / pytest / SQLAlchemy 2.0 / psycopg2-binary（已装）/ Docker Postgres 16。

## Global Constraints

- 不改任何生产代码（纯测试基础设施 + 测试）。
- **双后端都必须全绿**：默认 `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`（SQLite）
  与 `TZ=Asia/Shanghai TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade .venv/bin/python -m pytest -q`（PG）。
- 本地 PG 已起：容器 `gridpg`（`docker run -d --name gridpg -e POSTGRES_PASSWORD=grid -e POSTGRES_DB=gridtrade -p 5432:5432 postgres:16`）。
- 每个测试一份 store（PG 分支测试前 TRUNCATE 全表 RESTART IDENTITY CASCADE 做隔离）。
- 保留每个测试原意；尤其「同一 store 上建两个 executor 模拟重启」的跨进程测试，继续共用该 fixture store。
- 并发测试 PG-only（SQLite StaticPool 单连接造不出真竞态）。
- CI 不变（不加 PG job）。

---

### Task 1: 两个 fixture + 冒烟测试

**Files:**
- Modify: `tests/conftest.py`（追加 `_truncate_all` / `store` / `pg_store`）
- Create: `tests/state/test_pg_fixture.py`（双模式冒烟）

**Interfaces:**
- Consumes: `gridtrade.state.store.StateStore`（`from_url` / `in_memory` / `create_all` / `.engine`）、
  `gridtrade.state.models.metadata`。
- Produces:
  - `store` fixture：yield 一个建好表的 `StateStore`；PG 模式测试前 TRUNCATE、测试后 dispose。
  - `pg_store` fixture：无 `TEST_DATABASE_URL` 则 `pytest.skip`；否则 PG store（建表+TRUNCATE+dispose）。

- [ ] **Step 1: 写冒烟测试**

```python
# tests/state/test_pg_fixture.py
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import Grid


def test_store_fixture_roundtrips(store):
    repo = GridRepository(store)
    g = repo.create(Grid(id='', exchange='fake', symbol='BTC/USDT:USDT', status='PENDING'))
    assert repo.get(g.id).status == 'PENDING'


def test_store_fixture_isolated_between_tests(store):
    # 上一个测试建的 grid 不应残留（SQLite 天然新库；PG 靠 TRUNCATE）
    repo = GridRepository(store)
    assert repo.list_active() == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_pg_fixture.py -q`
Expected: FAIL —— `fixture 'store' not found`。

- [ ] **Step 3: 在 conftest.py 追加 fixture**

在 `tests/conftest.py` 末尾追加（保留文件已有的 TZ 设置）：

```python
import os
import pytest
from sqlalchemy import text

from gridtrade.state.store import StateStore
from gridtrade.state.models import metadata


def _truncate_all(st):
    names = ', '.join(t.name for t in metadata.sorted_tables)
    with st.engine.begin() as c:
        c.execute(text('TRUNCATE %s RESTART IDENTITY CASCADE' % names))


@pytest.fixture
def store():
    """双模式：TEST_DATABASE_URL 有值走 Postgres（每测 TRUNCATE 隔离），否则内存 SQLite。"""
    url = os.environ.get('TEST_DATABASE_URL')
    if url:
        st = StateStore.from_url(url)
        st.create_all()
        _truncate_all(st)
        yield st
        st.engine.dispose()
    else:
        st = StateStore.in_memory()
        st.create_all()
        yield st


@pytest.fixture
def pg_store():
    """PG-only：无 TEST_DATABASE_URL 则跳过（真并发测试用，SQLite 造不出真竞态）。"""
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('set TEST_DATABASE_URL to run Postgres-only tests')
    st = StateStore.from_url(url)
    st.create_all()
    _truncate_all(st)
    yield st
    st.engine.dispose()
```

- [ ] **Step 4: 跑测试确认通过（双后端）**

Run（SQLite）: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_pg_fixture.py -q`
Run（PG）: `TZ=Asia/Shanghai TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade .venv/bin/python -m pytest tests/state/test_pg_fixture.py -q`
Expected: 两次都 PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add tests/conftest.py tests/state/test_pg_fixture.py
git commit -m "test: dual-mode store + pg_store fixtures (PG via TEST_DATABASE_URL)"
```

---

### 重构通则（Task 2–4 共用）

把每个文件里「测试自建内存库」改为消费 `store` fixture。三种常见形态与改法：

**形态①：helper 返回 (..., store, ...)**
```python
# BEFORE
def _setup(price=100.0):
    ex = FakeExchange(...)
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(ex, store, ...)
    return ex, store, gx
def test_x():
    ex, store, gx = _setup()
# AFTER（helper 收 store 作首参、删自建、返回形状不变）
def _setup(store, price=100.0):
    ex = FakeExchange(...)
    gx = GridExecutor(ex, store, ...)
    return ex, store, gx
def test_x(store):
    ex, store, gx = _setup(store)
```

**形态②：helper 自建库并返回 repo**
```python
# BEFORE
def _grid_repo_with(*syms, exchange='okx'):
    s = StateStore.in_memory(); s.create_all()
    repo = GridRepository(s); ...; return repo
def test_x():
    repo = _grid_repo_with('BTC/USDT:USDT')
# AFTER
def _grid_repo_with(store, *syms, exchange='okx'):
    repo = GridRepository(store); ...; return repo
def test_x(store):
    repo = _grid_repo_with(store, 'BTC/USDT:USDT')
```

**形态③：测试体内联建库**
```python
# BEFORE
def test_x():
    store = StateStore.in_memory(); store.create_all()
    ...
# AFTER
def test_x(store):
    ...   # 删掉那两行，store 由 fixture 注入
```

规则要点：
- 凡出现 `StateStore.in_memory()` + `create_all()` 处一律删除，改由 `store` fixture 注入。
- 测试函数加 `store` 形参；helper 加 `store` 首参并由调用方传入；返回元组里原有的 `store` 保留（即注入的那个）。
- **一个测试只用一份 store**。若某测试原本在一个函数内 `StateStore.in_memory()` 调用**两次**且期望两个**相互隔离**的库——需识别并保留语义（多数情况是「两 executor 共享同一 store」，传同一个 fixture store 即可；若确为两套独立库，单独处理并在报告里说明）。
- 不再 import `StateStore` 的文件可顺手删去该 import（若仅此处用）。
- 每个 Task 末尾**双后端**各跑一次该目录，均须全绿。

---

### Task 2: 重构 tests/state（8 文件）

**Files（Modify）:** `tests/state/test_grids.py`, `test_orders.py`, `test_accounting.py`,
`test_records.py`, `test_fills.py`, `test_heartbeats.py`, `test_store_schema.py`,
`test_transition_revalidate.py`

**Interfaces:** Consumes Task 1 的 `store` fixture。Produces 无。

- [ ] **Step 1: 按「重构通则」逐文件改用 store fixture**

逐个打开上述 8 文件，定位 `StateStore.in_memory()`/`create_all()`，按形态①②③改。
`test_store_schema.py` 多半是形态③；`test_transition_revalidate.py` 注意其「串行 mutator」语义
（在同一 store 上手动改状态再调 transition）——继续用同一注入 store，仅删自建库两行。

- [ ] **Step 2: 双后端跑 state 目录**

Run（SQLite）: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state -q`
Run（PG）: `TZ=Asia/Shanghai TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade .venv/bin/python -m pytest tests/state -q`
Expected: 两次都全绿、用例数不减。若 PG 下出现 SQLite 没有的失败（如类型/约束），**停下汇报**——可能挖到真实 PG 兼容问题。

- [ ] **Step 3: 提交**

```bash
git add tests/state/
git commit -m "test(state): use dual-mode store fixture (8 files)"
```

---

### Task 3: 重构 tests/execution（12 文件）

**Files（Modify）:** `tests/execution/test_gates.py`, `test_grid_executor.py`,
`test_grid_executor_idempotent.py`, `test_manager.py`, `test_monitor.py`, `test_reconciler.py`,
`test_reconcile_orderid.py`, `test_sync_orderid.py`, `test_chaos_open.py`, `test_chaos_replenish.py`,
`test_chaos_reconcile.py`, `test_chaos_close.py`

**Interfaces:** Consumes Task 1 的 `store` fixture。Produces 无。

- [ ] **Step 1: 按「重构通则」逐文件改用 store fixture**

注意点：
- `test_gates.py`：`_grid_repo_with(...)` / `_grid_repo_with_caps(...)` 是形态②（加 store 首参）；
  另有 MarginGate 用例的 `_BalAdapter` 与 store 无关，不动。
- `test_reconciler.py`（4 处）：每个测试里 `gx` 与 `gx2` **共享同一 store** 模拟重启——
  两者都传同一注入 `store`，删掉那一行自建库即可，语义不变。
- 各 `test_chaos_*.py` / `test_manager.py` / `test_monitor.py`：多为形态①的 `build_stack`/`_setup`/`_new_executor`。
  把这些 helper 改为收 `store` 首参、删自建库、返回形状保留。

- [ ] **Step 2: 双后端跑 execution 目录**

Run（SQLite）: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution -q`
Run（PG）: `TZ=Asia/Shanghai TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade .venv/bin/python -m pytest tests/execution -q`
Expected: 两次都全绿、用例数不减。PG 下新失败 → 停下汇报。

- [ ] **Step 3: 提交**

```bash
git add tests/execution/
git commit -m "test(execution): use dual-mode store fixture (12 files)"
```

---

### Task 4: 重构 tests/runtime（3 文件）

**Files（Modify）:** `tests/runtime/test_cycles.py`, `test_chaos_cycle.py`, `test_dbadmin.py`

**Interfaces:** Consumes Task 1 的 `store` fixture。Produces 无。

- [ ] **Step 1: 按「重构通则」逐文件改用 store fixture**

注意点：
- `test_cycles.py` / `test_chaos_cycle.py`：形态①的 `_setup`/`build`（收 store 首参）。
- `test_dbadmin.py`（3 处）：测的是 dbadmin reset（drop+create）。若某测试需要「干净空库 + 自行 create/drop」，
  保留其对 store 的特殊操作语义，仅把「获得一个 store」改为 fixture 注入；不要破坏 reset 的断言。
  若该文件的某用例本质要自管 schema 生命周期、与 fixture 的 create_all 冲突，**停下汇报**由控制者裁决。

- [ ] **Step 2: 双后端跑 runtime 目录**

Run（SQLite）: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime -q`
Run（PG）: `TZ=Asia/Shanghai TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade .venv/bin/python -m pytest tests/runtime -q`
Expected: 两次都全绿、用例数不减。

- [ ] **Step 3: 全套双后端回归**

Run（SQLite）: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Run（PG）: `TZ=Asia/Shanghai TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade .venv/bin/python -m pytest -q`
Expected: 两次都全绿（SQLite 用例数 = 重构前 + Task1 的 2 个冒烟；PG 一致）。

- [ ] **Step 4: 提交**

```bash
git add tests/runtime/
git commit -m "test(runtime): use dual-mode store fixture (3 files)"
```

---

### Task 5: 真并发 TOCTOU 测试

**Files:**
- Create: `tests/state/test_transition_concurrency.py`

**Interfaces:** Consumes Task 1 的 `pg_store` fixture；`GridRepository.transition_status`、
`gridtrade.state.models`（`Grid`/`ACTIVE`/`OPENING`/`CLOSING`/`ConcurrencyError`/`StateError`）。

- [ ] **Step 1: 写并发测试**

```python
# tests/state/test_transition_concurrency.py
"""真并发 TOCTOU 测试：transition_status 单事务版本守卫在真线程竞争下只放一个赢家。
需 Postgres（SQLite StaticPool 单连接造不出真竞态）。跑法：
  docker run -d --name gridpg -e POSTGRES_PASSWORD=grid -e POSTGRES_DB=gridtrade -p 5432:5432 postgres:16
  TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade \
    .venv/bin/python -m pytest tests/state/test_transition_concurrency.py
"""
import threading

from gridtrade.state.grids import GridRepository
from gridtrade.state.models import (Grid, ACTIVE, OPENING, CLOSING,
                                    ConcurrencyError, StateError)


def _race(store, grid_id, expected_version, new_status, n):
    repo = GridRepository(store)
    barrier = threading.Barrier(n)
    wins, errors = [], []
    lock = threading.Lock()

    def worker():
        barrier.wait()                       # 所有线程同刻开打，最大化竞争
        try:
            g = repo.transition_status(grid_id, new_status,
                                       expected_version=expected_version)
            with lock:
                wins.append(g)
        except (ConcurrencyError, StateError) as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return wins, errors


def test_concurrent_same_version_exactly_one_winner(pg_store):
    repo = GridRepository(pg_store)
    g = repo.create(Grid(id='', exchange='fake', symbol='BTC/USDT:USDT', status='PENDING'))
    wins, errors = _race(pg_store, g.id, g.version, OPENING, n=8)
    assert len(wins) == 1                                  # 恰好一个赢
    assert len(errors) == 7                                # 其余全被拒
    # 每个输家都拿到语义合法的并发/状态错误（非静默成功、非崩溃）
    assert all(isinstance(e, (ConcurrencyError, StateError)) for e in errors)
    final = repo.get(g.id)
    assert final.status == 'OPENING'
    assert final.version == g.version + 1                  # 只 +1 一次，无双赢/丢更新


def test_concurrent_double_close_only_one_wins(pg_store):
    repo = GridRepository(pg_store)
    g = repo.create(Grid(id='', exchange='fake', symbol='ETH/USDT:USDT', status='PENDING'))
    g = repo.transition_status(g.id, OPENING, expected_version=g.version)
    g = repo.transition_status(g.id, ACTIVE, expected_version=g.version)
    wins, errors = _race(pg_store, g.id, g.version, CLOSING, n=2)
    assert len(wins) == 1 and len(errors) == 1             # 一赢一拒 -> 不双平
    assert isinstance(errors[0], (ConcurrencyError, StateError))
    final = repo.get(g.id)
    assert final.status == 'CLOSING'
    assert final.version == g.version + 1
```

- [ ] **Step 2: 跑测试（PG 必须；无 PG 应 skip）**

Run（无 env，应 skip）: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_transition_concurrency.py -q`
Expected: 2 skipped。
Run（PG）: `TZ=Asia/Shanghai TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade .venv/bin/python -m pytest tests/state/test_transition_concurrency.py -q`
Expected: 2 passed。若某条变红（出现双赢 / version 多次 +1 / 输家静默成功），说明守卫在真竞态下不成立——**停下汇报**（这才是这测试要抓的）。

- [ ] **Step 3: 提交**

```bash
git add tests/state/test_transition_concurrency.py
git commit -m "test(state): real-thread concurrency TOCTOU test for transition_status (PG-only)"
```

---

### Task 6: 文档 + 记忆

**Files:**
- Modify: `docs/STATUS.md`（§4 测试 + §9 延后项）、`deploy/DEPLOY.md`（PG 测试跑法）
- Modify: `/Users/thomaschang/.claude/projects/-Users-thomaschang-Projects-GridTradeGP/memory/deferred-toctou-concurrency-test.md` 与 `MEMORY.md` 指针

**Interfaces:** Consumes 前 5 任务结果。Produces 无。

- [ ] **Step 1: 更新 STATUS.md**

§4 测试段加一句：DB 测试支持双后端——默认 SQLite；`TEST_DATABASE_URL=postgresql://…` 切真 PG 全量跑；
并发 TOCTOU 测试 PG-only。把测试计数更新为新全套数（跑 `TZ=Asia/Shanghai .venv/bin/python -m pytest -q 2>&1 | tail -1` 取数）。
§9 延后项：把「真并发 TOCTOU 测试」从延后改为 `✅ 已补（本地 PG 真线程；CI 仍 SQLite，多机阶段再上 CI PG job）`。

- [ ] **Step 2: 更新 DEPLOY.md**

加一节「本地 PG 测试」：
```
docker run -d --name gridpg -e POSTGRES_PASSWORD=grid -e POSTGRES_DB=gridtrade -p 5432:5432 postgres:16
export TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade
TZ=Asia/Shanghai .venv/bin/python -m pytest -q     # 全量走 PG
unset TEST_DATABASE_URL                              # 回到默认 SQLite
```

- [ ] **Step 3: 更新记忆 deferred-toctou-concurrency-test**

把该记忆正文从「延后到多监控机阶段」改为「已补：`tests/state/test_transition_concurrency.py`
用真线程 + Barrier 在本地 PG 上验证 transition_status 单事务版本守卫只放一个赢家；CI 仍 SQLite，
CI PG job 留待多监控机阶段」。`MEMORY.md` 对应指针一行同步。

- [ ] **Step 4: 提交**

```bash
git add docs/STATUS.md deploy/DEPLOY.md
git commit -m "docs: dual-backend test runner + concurrency TOCTOU now covered"
```

---

## 自检（Self-Review）

- **Spec 覆盖**：两 fixture → Task 1；23 文件重构（state 8 / execution 12 / runtime 3）→ Task 2/3/4 + 重构通则；
  并发 A/B 场景 → Task 5；文档/记忆 → Task 6；双后端全绿验收 → 各 Task Step 2 + Task 4 Step 3 显式双跑。覆盖完整。
- **占位符**：fixture 与并发测试给完整代码；重构任务给「通则 + 三形态 worked example + 文件清单 + 双后端验证」——
  机械统一变换以模式而非逐文件全文指定（实现者按通则读每个文件套用），非占位。
- **类型/命名一致**：`store`/`pg_store`/`_truncate_all`/`TEST_DATABASE_URL` 全程一致；并发测试 import 的
  `ConcurrencyError/StateError/OPENING/ACTIVE/CLOSING/Grid` 均来自 `gridtrade.state.models`（与 grids.py 同源）。
- **风险显式标注**：PG 下新失败、dbadmin schema 生命周期冲突、单测多独立库——三处都写了「停下汇报」。
