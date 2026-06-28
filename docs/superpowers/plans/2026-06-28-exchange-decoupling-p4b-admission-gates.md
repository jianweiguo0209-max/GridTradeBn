# 交易所解耦重构 P4b 实现计划（准入门链 Chain of Responsibility — 无冲突第一道闸）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现「触发 → 准入 → 执行」三段式里的**准入门链**（design.md §6②）：每个开网格提议（`GridProposal`）必须依次过闸，任一门拒绝即短路并带原因。本增量交付可插拔门接口 `AdmissionGate` + 链 `GateChain` + 两个无歧义结构门 `SymbolLockGate`（币种互斥）、`MaxConcurrentGate`（并发上限）。`MarginGate`/`RiskBudgetGate` 因需保证金/风险敞口口径决策，本增量只留接口、显式延后。

**Architecture:** 纯决策组件，全程离线可测。`GridProposal` 是触发器产出、执行器消费的开网格提议数据类（字段对齐 `GridExecutor.open` 入参）。每个门是 `AdmissionGate` 子类，依赖通过构造注入（DI），`check(proposal) -> GateResult`。`GateChain` 顺序执行门、返回首个失败或全过；`filter` 批量筛选可放行提议。门只读状态（`GridRepository.get_active_by_symbol / list_active`），不写、不下单。

**Tech Stack:** Python 3.9、dataclasses、abc、SQLAlchemy 2.0（间接经 GridRepository）、pytest、内存 SQLite。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（提议字段口径、门语义、链短路顺序、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；`gridtrade/execution/` 与 `gridtrade/state/` 不得 import 交易所库（门只经 `GridRepository` 读状态；本增量门不碰 adapter）。
- 只新增 `gridtrade/execution/gates.py` 及 `tests/execution/test_gates.py`；不改 `state/`、`core/`、`exchanges/`、已有 `execution/*`、`backtest/`。
- `GridProposal` 字段须能直接喂 `GridExecutor.open(exchange, symbol, grid_params, *, offset=0, tag='')`：`grid_params` 为含 `low_price/high_price/grid_count/stop_low_price/stop_high_price` 的 dict。
- 门只读不写：用 `GridRepository.get_active_by_symbol(exchange, symbol)`、`list_active()`；不调用任何写方法、不下单。
- 一期隔离模型 `SymbolExclusivePolicy`：同一 `(exchange, symbol)` 同时只允许一个活跃网格 → `SymbolLockGate` 提议级优雅拒绝（DB 唯一约束是兜底，二者互补）。
- `MarginGate`、`RiskBudgetGate` 本增量**不实现**（保证金模型/风险敞口口径待用户决策）；`AdmissionGate` 接口须留得下它们（`check(proposal) -> GateResult`，未来门可在构造注入 adapter）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 124 passed）。

---

## 文件结构（本计划新建）

```
gridtrade/execution/
  gates.py        # 新增：GridProposal / GateResult / AdmissionGate(ABC) / GateChain
                  #       + SymbolLockGate + MaxConcurrentGate
tests/execution/
  test_gates.py   # 新增：链短路/全过/filter + 两个门的红-绿
```

`gates.py` 公共接口（供 P4c 触发器、P4d GridManager 消费）：

```python
@dataclass
class GridProposal:
    exchange: str
    symbol: str
    grid_params: dict           # low_price/high_price/grid_count/stop_low_price/stop_high_price
    offset: int = 0
    tag: str = ''
    cap: Optional[float] = None # 预留：未来 MarginGate 用（None=用执行器默认 cap）
    source: str = ''            # 溯源：哪个触发器提议的

@dataclass
class GateResult:
    passed: bool
    gate: str
    reason: str = ''

class AdmissionGate(ABC):
    @abstractmethod
    def check(self, proposal: GridProposal) -> GateResult: ...

class GateChain:
    def __init__(self, gates: Iterable[AdmissionGate]): ...
    def evaluate(self, proposal: GridProposal) -> GateResult: ...   # 首个失败或全过
    def filter(self, proposals: Iterable[GridProposal]) -> List[GridProposal]: ...

class SymbolLockGate(AdmissionGate):
    def __init__(self, grids: GridRepository): ...

class MaxConcurrentGate(AdmissionGate):
    def __init__(self, grids: GridRepository, max_concurrent: int): ...
```

---

### Task 1: GridProposal / GateResult / AdmissionGate / GateChain（链语义）

**Files:**
- Create: `gridtrade/execution/gates.py`
- Create: `tests/execution/test_gates.py`

**Interfaces:**
- Produces: `GridProposal`、`GateResult`、`AdmissionGate`(ABC)、`GateChain`（见上签名）。
- Consumes: 仅标准库 `dataclasses / abc / typing`。

- [ ] **Step 1: 写失败测试**

Create `tests/execution/test_gates.py`:

```python
import pytest

from gridtrade.execution.gates import (GridProposal, GateResult, AdmissionGate,
                                       GateChain)


def _proposal(symbol='BTC/USDT:USDT', **kw):
    base = dict(exchange='okx', symbol=symbol,
                grid_params={'low_price': 90.0, 'high_price': 110.0,
                             'grid_count': 10, 'stop_low_price': 85.0,
                             'stop_high_price': 115.0})
    base.update(kw)
    return GridProposal(**base)


class _AlwaysPass(AdmissionGate):
    def check(self, proposal):
        return GateResult(True, 'AlwaysPass')


class _AlwaysFail(AdmissionGate):
    def __init__(self, reason='nope'):
        self.reason = reason
    def check(self, proposal):
        return GateResult(False, 'AlwaysFail', self.reason)


def test_chain_all_pass_returns_passed():
    chain = GateChain([_AlwaysPass(), _AlwaysPass()])
    r = chain.evaluate(_proposal())
    assert r.passed is True


def test_chain_short_circuits_on_first_failure():
    # 第二门失败；第三门即便会失败也不应被求值（用 raise 哨兵证明短路）
    class _Boom(AdmissionGate):
        def check(self, proposal):
            raise AssertionError('should not be evaluated after a failure')
    chain = GateChain([_AlwaysPass(), _AlwaysFail('blocked'), _Boom()])
    r = chain.evaluate(_proposal())
    assert r.passed is False
    assert r.gate == 'AlwaysFail' and r.reason == 'blocked'


def test_chain_filter_keeps_only_passing_proposals():
    # 偶数索引放行、奇数拦截，验证 filter 批量语义
    class _BySymbol(AdmissionGate):
        def check(self, proposal):
            ok = proposal.symbol != 'ETH/USDT:USDT'
            return GateResult(ok, 'BySymbol', '' if ok else 'eth blocked')
    chain = GateChain([_BySymbol()])
    props = [_proposal('BTC/USDT:USDT'), _proposal('ETH/USDT:USDT'),
             _proposal('SOL/USDT:USDT')]
    kept = chain.filter(props)
    assert [p.symbol for p in kept] == ['BTC/USDT:USDT', 'SOL/USDT:USDT']


def test_empty_chain_passes():
    assert GateChain([]).evaluate(_proposal()).passed is True
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_gates.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.gates`）。

- [ ] **Step 3: 实现 gates.py 骨架**

Create `gridtrade/execution/gates.py`:

```python
"""准入门链（Chain of Responsibility）—— 开网格提议的无冲突第一道闸。

触发器产出 GridProposal -> GateChain 顺序过闸 -> 放行的提议交 GridManager 开仓。
门只读状态、不下单、不写库。MarginGate/RiskBudgetGate 待保证金/风险口径决策后补。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, List, Optional


@dataclass
class GridProposal:
    exchange: str
    symbol: str
    grid_params: dict
    offset: int = 0
    tag: str = ''
    cap: Optional[float] = None
    source: str = ''


@dataclass
class GateResult:
    passed: bool
    gate: str
    reason: str = ''


class AdmissionGate(ABC):
    @abstractmethod
    def check(self, proposal: GridProposal) -> GateResult:
        ...


class GateChain:
    def __init__(self, gates: Iterable[AdmissionGate]):
        self.gates: List[AdmissionGate] = list(gates)

    def evaluate(self, proposal: GridProposal) -> GateResult:
        for gate in self.gates:
            result = gate.check(proposal)
            if not result.passed:
                return result
        return GateResult(True, 'GateChain')

    def filter(self, proposals: Iterable[GridProposal]) -> List[GridProposal]:
        return [p for p in proposals if self.evaluate(p).passed]
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_gates.py -v`
Expected: 4 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/gates.py tests/execution/test_gates.py
git commit -m "feat(execution): GridProposal + admission GateChain skeleton (P4b)"
```

---

### Task 2: SymbolLockGate（币种互斥，一期 SymbolExclusivePolicy）

**Files:**
- Modify: `gridtrade/execution/gates.py`
- Modify: `tests/execution/test_gates.py`

**Interfaces:**
- Consumes: `gridtrade.state.grids.GridRepository.get_active_by_symbol(exchange, symbol) -> Optional[Grid]`。
- Produces: `SymbolLockGate(grids: GridRepository)`。

- [ ] **Step 1: 写失败测试**

在 `tests/execution/test_gates.py` 末尾追加：

```python
def _grid_repo_with(*active_symbols, exchange='okx'):
    from gridtrade.state.store import StateStore
    from gridtrade.state.grids import GridRepository
    from gridtrade.state.models import Grid, ACTIVE
    s = StateStore.in_memory(); s.create_all()
    repo = GridRepository(s)
    for sym in active_symbols:
        repo.create(Grid(id='', exchange=exchange, symbol=sym, status=ACTIVE))
    return repo


def test_symbol_lock_blocks_when_active_grid_exists():
    from gridtrade.execution.gates import SymbolLockGate
    repo = _grid_repo_with('BTC/USDT:USDT')
    gate = SymbolLockGate(repo)
    r = gate.check(_proposal('BTC/USDT:USDT'))
    assert r.passed is False and r.gate == 'SymbolLockGate'


def test_symbol_lock_allows_free_symbol():
    from gridtrade.execution.gates import SymbolLockGate
    repo = _grid_repo_with('BTC/USDT:USDT')
    gate = SymbolLockGate(repo)
    assert gate.check(_proposal('ETH/USDT:USDT')).passed is True


def test_symbol_lock_is_per_exchange():
    from gridtrade.execution.gates import SymbolLockGate
    repo = _grid_repo_with('BTC/USDT:USDT', exchange='okx')
    gate = SymbolLockGate(repo)
    # 同币种但不同交易所 -> 放行
    assert gate.check(_proposal('BTC/USDT:USDT', exchange='hyperliquid')).passed is True
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_gates.py -k symbol_lock -v`
Expected: FAIL（`ImportError: cannot import name 'SymbolLockGate'`）。

- [ ] **Step 3: 实现 SymbolLockGate**

在 `gridtrade/execution/gates.py` 末尾追加：

```python
class SymbolLockGate(AdmissionGate):
    """一期 SymbolExclusivePolicy：同 (exchange, symbol) 已有活跃网格则拒绝。"""

    def __init__(self, grids):
        self.grids = grids

    def check(self, proposal: GridProposal) -> GateResult:
        existing = self.grids.get_active_by_symbol(proposal.exchange,
                                                   proposal.symbol)
        if existing is not None:
            return GateResult(False, 'SymbolLockGate',
                              'active grid already exists for %s on %s'
                              % (proposal.symbol, proposal.exchange))
        return GateResult(True, 'SymbolLockGate')
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_gates.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/gates.py tests/execution/test_gates.py
git commit -m "feat(execution): SymbolLockGate per-exchange symbol exclusivity (P4b)"
```

---

### Task 3: MaxConcurrentGate（并发网格上限）

**Files:**
- Modify: `gridtrade/execution/gates.py`
- Modify: `tests/execution/test_gates.py`

**Interfaces:**
- Consumes: `gridtrade.state.grids.GridRepository.list_active() -> List[Grid]`（状态属于 ACTIVE_STATES 的网格）。
- Produces: `MaxConcurrentGate(grids: GridRepository, max_concurrent: int)`。

- [ ] **Step 1: 写失败测试**

在 `tests/execution/test_gates.py` 末尾追加：

```python
def test_max_concurrent_blocks_at_limit():
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with('BTC/USDT:USDT', 'ETH/USDT:USDT')
    gate = MaxConcurrentGate(repo, max_concurrent=2)
    r = gate.check(_proposal('SOL/USDT:USDT'))
    assert r.passed is False and r.gate == 'MaxConcurrentGate'


def test_max_concurrent_allows_below_limit():
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with('BTC/USDT:USDT')
    gate = MaxConcurrentGate(repo, max_concurrent=2)
    assert gate.check(_proposal('SOL/USDT:USDT')).passed is True


def test_max_concurrent_zero_active_allows():
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with()
    gate = MaxConcurrentGate(repo, max_concurrent=1)
    assert gate.check(_proposal('BTC/USDT:USDT')).passed is True
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_gates.py -k max_concurrent -v`
Expected: FAIL（`ImportError: cannot import name 'MaxConcurrentGate'`）。

- [ ] **Step 3: 实现 MaxConcurrentGate**

在 `gridtrade/execution/gates.py` 末尾追加：

```python
class MaxConcurrentGate(AdmissionGate):
    """活跃网格数达到 max_concurrent 时拒绝新提议。"""

    def __init__(self, grids, max_concurrent: int):
        self.grids = grids
        self.max_concurrent = int(max_concurrent)

    def check(self, proposal: GridProposal) -> GateResult:
        active = len(self.grids.list_active())
        if active >= self.max_concurrent:
            return GateResult(False, 'MaxConcurrentGate',
                              'active grids %d >= limit %d'
                              % (active, self.max_concurrent))
        return GateResult(True, 'MaxConcurrentGate')
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_gates.py -v`
Expected: 全 PASS。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 124 + 新增门测试）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/gates.py tests/execution/test_gates.py
git commit -m "feat(execution): MaxConcurrentGate active-grid limit (P4b)"
```

---

## Self-Review

- **Spec 覆盖**：design.md §6② 准入门链（Chain of Responsibility）—— `GateChain`（Task 1）+ `SymbolLockGate`（Task 2，对应一期 SymbolExclusivePolicy）+ `MaxConcurrentGate`（Task 3）。`MarginGate/RiskBudgetGate` 显式延后（需口径决策），接口已留（`AdmissionGate.check`，未来门构造可注入 adapter）。`GridProposal` 字段对齐 `GridExecutor.open` 入参。
- **Placeholder 扫描**：无 TBD/TODO；每步给出完整代码与精确命令/预期。
- **类型一致**：`GateResult(passed, gate, reason)` 在链与三个门中字段一致；`SymbolLockGate`/`MaxConcurrentGate` 构造参数与「公共接口」声明一致；`GridProposal.grid_params` 键与 `GridExecutor.open` 读取的键一致（low_price/high_price/grid_count/stop_low_price/stop_high_price）。
- **延后项记录**：MarginGate/RiskBudgetGate 口径决策——见交付后向用户确认。
