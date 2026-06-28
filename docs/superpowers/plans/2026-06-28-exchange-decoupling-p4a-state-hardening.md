# 交易所解耦重构 P4a 实现计划（状态层评审遗留收尾：读路径 connect + transition_status 事务内重校验）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收尾 P2 最终评审记录的两项 P4 carry-forward 硬性要求，使状态层在真实 Postgres 上正确：① 所有纯读方法从写事务 `engine.begin()` 改为读连接 `engine.connect()`（避免不必要写锁）；② `GridRepository.transition_status` 把状态机校验搬进与版本守卫写同一个事务内（事务内重校验），消除 TOCTOU——并发下对已进入非法源态的网格返回语义正确的 `StateError` 而非泛化的 `ConcurrencyError`。

**Architecture:** 纯读方法（`get/list_*/max_ts/get_active_by_symbol`）只跑 SELECT，无需写事务；改用 `engine.connect()`（SQLAlchemy 2.0 future 风格，自动开读连接、退出即释放）。`transition_status` 当前是「事务外读校验 + 事务内版本守卫写」两段式（TOCTOU）；改为单事务：同一 `engine.begin()` 内先 SELECT 当前行、`can_transition` 重校验、再带 `version` 守卫 UPDATE，使校验数据与写在同一事务快照内一致。

**Tech Stack:** Python 3.9、SQLAlchemy 2.0 Core（future=True）、pytest、内存 SQLite（StaticPool）。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（哪些方法算纯读、重校验语义、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；SQLAlchemy 2.0 Core 风格（`future=True`）；测试针对内存 SQLite（StaticPool），无外部网络。
- 只改 `gridtrade/state/{grids,accounting,records,orders,fills}.py` 及新增 `tests/state/test_transition_revalidate.py`；不改 `models.py`、不改 `execution/`、`backtest/`、`core/`、`exchanges/`。
- 纯读方法（只含 SELECT，无 INSERT/UPDATE/DELETE）一律 `with self.engine.connect() as c:`；写方法（含 `transition_status` 在内的所有写）保持 `with self.engine.begin() as c:`。
- `transition_status` 改造后行为保持：非法转换 → `StateError`；行不存在 → `ConcurrencyError`；版本陈旧但转换合法 → `ConcurrencyError`。新增：并发下源态已变为非法时 → `StateError`（而非 `ConcurrencyError`）。
- 乐观锁语义不变：UPDATE 仍以传入的 `expected_version` 作版本守卫，`rowcount == 0` → `ConcurrencyError`。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。
- 全程绿：本计划任一 Task 完成后 `TZ=Asia/Shanghai .venv/bin/python -m pytest` 全量必须保持通过（基线 120 passed）。

---

## 文件结构（本计划修改/新建）

```
gridtrade/state/
  grids.py        # 修改：get / get_active_by_symbol / list_active 改 connect()；transition_status 单事务重校验
  accounting.py   # 修改：get 改 connect()
  records.py      # 修改：get / list_by_tag / list_by_grid 改 connect()
  orders.py       # 修改：get / list_by_grid 改 connect()
  fills.py        # 修改：list_by_grid / max_ts 改 connect()
tests/state/test_transition_revalidate.py   # 新增：事务内重校验的红-绿测试
```

读路径清单（需 `begin()` → `connect()`）：

| 文件 | 方法 | 行（修改前） |
|---|---|---|
| grids.py | get | 40 |
| grids.py | get_active_by_symbol | 45 |
| grids.py | list_active | 53 |
| accounting.py | get | 36 |
| records.py | get | 32 |
| records.py | list_by_tag | 39 |
| records.py | list_by_grid | 47 |
| orders.py | get | 44 |
| orders.py | list_by_grid | 51 |
| fills.py | list_by_grid | 32 |
| fills.py | max_ts | 39 |

写路径（**保持 `begin()` 不动**）：`grids.create`、`grids.transition_status`、`accounting.init/save`、`records.add`、`orders.upsert`、`fills.add_if_new`。`accounting.bump_peak`/`orders.list_open_by_grid` 不直接持连接（经 get/list 间接），不改。

---

### Task 1: transition_status 事务内重校验（消除 TOCTOU）

**Files:**
- Modify: `gridtrade/state/grids.py:59-91`
- Create: `tests/state/test_transition_revalidate.py`

**Interfaces:**
- Consumes: `gridtrade.state.models.{Grid, grids, can_transition, ACTIVE_STATES, TERMINAL_STATES, ConcurrencyError, StateError, now_ms, ACTIVE, FAILED, CLOSING, OPENING, PENDING}`、`gridtrade.state.grids.GridRepository`。
- Produces: `GridRepository.transition_status(grid_id, new_status, *, expected_version)` 签名不变；语义新增「并发源态非法 → StateError」。

- [ ] **Step 1: 写失败测试**

Create `tests/state/test_transition_revalidate.py`:

```python
import pytest

from gridtrade.state.models import (Grid, ACTIVE, OPENING, CLOSING, FAILED,
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


def test_stale_caller_on_now_terminal_grid_raises_state_error():
    """A 持 ACTIVE 旧版本想转 CLOSING；其间 B 已把网格转到 FAILED（终态）。
    事务内重校验应看到 FAILED 源态、can_transition(FAILED,CLOSING)=False，
    抛 StateError（语义正确：不能从终态转出），而非泛化 ConcurrencyError。"""
    repo = _repo()
    g = repo.create(_grid(status=OPENING))
    active = repo.transition_status(g.id, ACTIVE, expected_version=g.version)
    stale_version = active.version  # A 在此刻读到的版本
    # B 并发把网格推进到终态 FAILED（版本+1）
    repo.transition_status(active.id, FAILED, expected_version=active.version)
    # A 用陈旧版本尝试 ACTIVE->CLOSING：源态实际已是 FAILED -> StateError
    with pytest.raises(StateError):
        repo.transition_status(g.id, CLOSING, expected_version=stale_version)


def test_legal_transition_stale_version_still_concurrency_error():
    """源态仍合法、仅版本陈旧 -> 仍是 ConcurrencyError（乐观锁不破）。"""
    repo = _repo()
    g = repo.create(_grid(status=OPENING))
    repo.transition_status(g.id, ACTIVE, expected_version=g.version)
    # OPENING->ACTIVE 合法但 expected_version 早已过期
    with pytest.raises(ConcurrencyError):
        repo.transition_status(g.id, ACTIVE, expected_version=g.version)


def test_missing_grid_raises_concurrency_error():
    repo = _repo()
    with pytest.raises(ConcurrencyError):
        repo.transition_status('nope', ACTIVE, expected_version=1)
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_transition_revalidate.py -v`
Expected: `test_stale_caller_on_now_terminal_grid_raises_state_error` FAIL（现实现抛 ConcurrencyError，因为旧码用事务外陈旧读 ACTIVE 通过 can_transition，再撞版本守卫 rowcount==0）。其余两测可能已通过。

- [ ] **Step 3: 实现单事务重校验**

替换 `gridtrade/state/grids.py` 中 `transition_status`（第 59-91 行）为：

```python
    def transition_status(self, grid_id: str, new_status: str, *,
                          expected_version: int) -> Grid:
        # 单事务内：重读源态 -> can_transition 重校验 -> 版本守卫写。
        # 校验与写共享同一事务快照，消除「事务外读校验 + 事务内写」的 TOCTOU。
        # 并发下源态若已变为非法（如已进终态），重校验直接抛 StateError（语义正确），
        # 不再被泛化成 ConcurrencyError。版本守卫仍以传入 expected_version 为准。
        with self.engine.begin() as c:
            row = c.execute(select(grids).where(grids.c.id == grid_id)).first()
            if row is None:
                raise ConcurrencyError(f'grid {grid_id} not found')
            current = _to_grid(row)
            if not can_transition(current.status, new_status):
                raise StateError(
                    f'illegal transition {current.status} -> {new_status}')
            # Terminal -> release slot (NULL). Active state -> (re)claim symbol slot.
            if new_status in TERMINAL_STATES:
                active_symbol = None
            elif new_status in ACTIVE_STATES:
                active_symbol = current.symbol
            else:
                active_symbol = (current.symbol
                                 if current.status in ACTIVE_STATES else None)
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

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_transition_revalidate.py tests/state/test_grids.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/state/grids.py tests/state/test_transition_revalidate.py
git commit -m "fix(state): transition_status single-transaction revalidation (P4a TOCTOU)"
```

---

### Task 2: 纯读方法改 engine.connect()（避免真实 Postgres 写锁）

**Files:**
- Modify: `gridtrade/state/grids.py`（get / get_active_by_symbol / list_active）
- Modify: `gridtrade/state/accounting.py`（get）
- Modify: `gridtrade/state/records.py`（get / list_by_tag / list_by_grid）
- Modify: `gridtrade/state/orders.py`（get / list_by_grid）
- Modify: `gridtrade/state/fills.py`（list_by_grid / max_ts）

**Interfaces:**
- Consumes: 各仓储既有 `select(...)` 读语句；`store.engine`。
- Produces: 上述方法签名/返回值完全不变；仅事务上下文从 `begin()` → `connect()`。

> 说明：此为纯重构（无行为变化）。在 SQLite 上读路径用 `begin()` 与 `connect()` 结果一致，价值在真实 Postgres（读连接不取写锁）。因此**不写新行为测试**——验证手段是全量回归保持绿。这是评审记录的明确遗留项，不是新功能。

- [ ] **Step 1: 改 grids.py 三个读方法**

把 `get`、`get_active_by_symbol`、`list_active` 三处的 `with self.engine.begin() as c:` 改为 `with self.engine.connect() as c:`（仅这三个；`create` 与 `transition_status` 保持 `begin()`）。

- [ ] **Step 2: 改 accounting.py**

把 `get`（第 36 行）的 `with self.engine.begin() as c:` 改为 `with self.engine.connect() as c:`（`init`/`save` 保持 `begin()`）。

- [ ] **Step 3: 改 records.py**

把 `get`、`list_by_tag`、`list_by_grid` 三处 `with self.engine.begin() as c:` 改为 `with self.engine.connect() as c:`（`add` 保持 `begin()`）。

- [ ] **Step 4: 改 orders.py**

把 `get`、`list_by_grid` 两处 `with self.engine.begin() as c:` 改为 `with self.engine.connect() as c:`（`upsert` 保持 `begin()`）。

- [ ] **Step 5: 改 fills.py**

把 `list_by_grid`、`max_ts` 两处 `with self.engine.begin() as c:` 改为 `with self.engine.connect() as c:`（`add_if_new` 保持 `begin()`）。

- [ ] **Step 6: 自检无残留 + 全量回归**

确认写方法仍用 `begin()`、读方法全部 `connect()`：

```bash
grep -rn "engine.begin\|engine.connect" gridtrade/state/
```

Expected: `begin()` 仅出现在 create / transition_status / init / save / add / upsert / add_if_new；其余读方法均为 `connect()`。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 120 passed），无新增失败。

- [ ] **Step 7: 提交**

```bash
git add gridtrade/state/grids.py gridtrade/state/accounting.py gridtrade/state/records.py gridtrade/state/orders.py gridtrade/state/fills.py
git commit -m "refactor(state): read paths use engine.connect() not begin() (P4a, no write locks on Postgres)"
```

---

## Self-Review

- **Spec 覆盖**：design.md:223 的两项 P4 carry-forward —— ① 读路径 `connect()`（Task 2 覆盖全部 11 个读方法）；② `transition_status` 事务内重校验（Task 1）。均有对应 Task。
- **Placeholder 扫描**：无 TBD/TODO；每个写代码步给出完整代码或精确的「哪一行、改成什么」指令。
- **类型一致**：`transition_status` 签名与返回 `Grid` 不变；异常类型沿用 `models` 中既有 `StateError/ConcurrencyError`；读方法签名零变更。
