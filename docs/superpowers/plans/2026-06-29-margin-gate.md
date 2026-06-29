# MarginGate（可用保证金准入门）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现准入门链第 4 道门 `MarginGate`——实时查交易所可用余额，确保 `可用 cash ≥ 本提议所需 cap`（同轮累计扣减、fail-closed），并接入 factory 门链末尾。

**Architecture:** `MarginGate` 注入 adapter，每轮 `GateChain.filter` 开始时经新的 `begin_batch()` 钩子快照一次 `fetch_balance().cash`、清零本轮预留；每放行一个提议就扣减其所需，防同轮超额。门置于链尾（短路链中过它即准入，预留不虚高）。

**Tech Stack:** Python 3.9 / pytest。

## Global Constraints

- 跑测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest`（仓库 `.venv`）。
- 现有全套（当前 279）测试零回归；改动以加法为主（`begin_batch` 默认空实现，不影响其余三门）。
- 口径（用户敲定）：所需 = `proposal.cap`（未给则 `default_cap`）；放行条件 `可用 − 已预留 ≥ 所需`；
  比对 `Balance.cash`；fail-closed（余额读不到 → 全拒）。
- `gridtrade/execution/` 不 import ccxt。
- MarginGate 必须位于门链**末尾**（累计「放行即预留」的正确性前提）。

---

### Task 1: MarginGate + begin_batch 批次钩子

**Files:**
- Modify: `gridtrade/execution/gates.py`（`AdmissionGate` 加 `begin_batch`；`GateChain.filter` 调它；新增 `MarginGate`）
- Test: `tests/execution/test_gates.py`（门单测 + 链集成）

**Interfaces:**
- Consumes: `adapter.fetch_balance() -> Balance(equity, cash)`（`gridtrade.exchanges.base.Balance`）。
- Produces:
  - `AdmissionGate.begin_batch(self) -> None`（默认空实现）。
  - `GateChain.filter` 在遍历前对每个门调一次 `begin_batch()`。
  - `MarginGate(adapter, default_cap)`：`check(proposal) -> GateResult`，`gate` 名为 `'MarginGate'`；
    余额不可读时 `reason='balance unavailable'`。

- [ ] **Step 1: 写失败测试**

在 `tests/execution/test_gates.py` 末尾追加：

```python
class _BalAdapter:
    """最小余额桩：可配可用余额；raises=True 模拟 fetch_balance 抛错。"""
    def __init__(self, cash, raises=False):
        self._cash = cash; self._raises = raises; self.calls = 0
    def fetch_balance(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError('balance down')
        from gridtrade.exchanges.base import Balance
        return Balance(equity=self._cash, cash=self._cash)


def test_margin_gate_allows_when_cash_ge_cap():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(100.0), default_cap=100.0)
    assert gate.check(_proposal()).passed is True       # 惰性快照, 100>=100


def test_margin_gate_blocks_when_cash_below_required():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(50.0), default_cap=100.0)
    r = gate.check(_proposal())
    assert r.passed is False and r.gate == 'MarginGate'


def test_margin_gate_uses_explicit_proposal_cap():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(30.0), default_cap=100.0)
    assert gate.check(_proposal(cap=20.0)).passed is True   # 用显式 20 而非 default 100


def test_margin_gate_cumulative_deduction():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(250.0), default_cap=100.0)
    gate.begin_batch()
    assert gate.check(_proposal()).passed is True       # reserve 100, 余 150
    assert gate.check(_proposal()).passed is True       # reserve 200, 余 50
    r = gate.check(_proposal())                          # 需 100 > 50 -> 拒
    assert r.passed is False and r.gate == 'MarginGate'


def test_margin_gate_begin_batch_resets_reservation():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(100.0), default_cap=100.0)
    gate.begin_batch()
    assert gate.check(_proposal()).passed is True        # reserve 100
    assert gate.check(_proposal()).passed is False       # 余 0 < 100
    gate.begin_batch()                                   # 新批: 预留清零 + 重拉余额
    assert gate.check(_proposal()).passed is True        # 又 100>=100


def test_margin_gate_fail_closed_on_balance_error():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(1000.0, raises=True), default_cap=100.0)
    gate.begin_batch()
    r = gate.check(_proposal())
    assert r.passed is False and r.reason == 'balance unavailable'


def test_margin_gate_in_chain_filter_cumulative_and_per_batch_snapshot():
    from gridtrade.execution.gates import MarginGate
    adapter = _BalAdapter(250.0)
    chain = GateChain([MarginGate(adapter, default_cap=100.0)])
    props = [_proposal('BTC/USDT:USDT'), _proposal('ETH/USDT:USDT'),
             _proposal('SOL/USDT:USDT')]
    kept = chain.filter(props)
    assert [p.symbol for p in kept] == ['BTC/USDT:USDT', 'ETH/USDT:USDT']  # 第三超额被拒
    kept2 = chain.filter(props)                           # 新批 -> begin_batch 重置
    assert len(kept2) == 2
    assert adapter.calls == 2                             # 每批仅快照一次余额
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_gates.py -q`
Expected: 新增用例 FAIL —— `ImportError: cannot import name 'MarginGate'`。

- [ ] **Step 3a: 给 AdmissionGate 加 begin_batch、GateChain.filter 调它**

在 `gridtrade/execution/gates.py` 中，把 `AdmissionGate` 改为：

```python
class AdmissionGate(ABC):
    def begin_batch(self) -> None:
        """每轮 filter 开始前调用一次；有状态门（如 MarginGate）借此重置批次状态。默认空。"""
        return None

    @abstractmethod
    def check(self, proposal: GridProposal) -> GateResult:
        ...
```

把 `GateChain.filter` 改为（遍历前先对每个门 begin_batch；先 list 化以支持生成器）：

```python
    def filter(self, proposals: Iterable[GridProposal]) -> List[GridProposal]:
        proposals = list(proposals)
        for gate in self.gates:
            gate.begin_batch()
        return [p for p in proposals if self.evaluate(p).passed]
```

- [ ] **Step 3b: 新增 MarginGate**

在 `gridtrade/execution/gates.py` 的 `RiskBudgetGate` 之后追加：

```python
class MarginGate(AdmissionGate):
    """可用保证金门：实时查交易所可用余额(cash) >= 本提议所需(cap)，同轮累计扣减。

    口径（用户敲定）：所需=proposal.cap（未给用 default_cap）；放行条件 cash - 已预留 >= 所需；
    fail-closed（余额读不到则全拒）。须置于门链末尾（短路链中过它即准入，预留不虚高）。
    """

    def __init__(self, adapter, default_cap):
        self.adapter = adapter
        self.default_cap = float(default_cap)
        self._available = None      # 本批可用余额快照；None=未快照
        self._reserved = 0.0        # 本批已放行提议的累计所需
        self._balance_ok = True

    def begin_batch(self) -> None:
        self._reserved = 0.0
        try:
            self._available = float(self.adapter.fetch_balance().cash)
            self._balance_ok = True
        except Exception:           # fail-closed：余额读不到 -> 本批全拒（绝不吞 BaseException）
            self._available = 0.0
            self._balance_ok = False

    def check(self, proposal: GridProposal) -> GateResult:
        if self._available is None:     # 未经 begin_batch 的独立 evaluate -> 惰性快照一次
            self.begin_batch()
        if not self._balance_ok:
            return GateResult(False, 'MarginGate', 'balance unavailable')
        required = (proposal.cap if proposal.cap is not None else self.default_cap)
        if self._available - self._reserved < required:
            return GateResult(False, 'MarginGate',
                              'free cash %.4f - reserved %.4f < required %.4f'
                              % (self._available, self._reserved, required))
        self._reserved += required
        return GateResult(True, 'MarginGate')
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_gates.py -q`
Expected: PASS（含既有门用例不受影响——`begin_batch` 默认空、filter 加法）。

- [ ] **Step 5: 跑全套确认无回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿（≥286 passed）。注意 `tests/runtime/test_factory.py::test_build_runtime_gate_chain_has_three_gates` **此时仍应通过**（factory 尚未接 MarginGate，链仍 3 门）——Task 2 才改它。

- [ ] **Step 6: 提交**

```bash
git add gridtrade/execution/gates.py tests/execution/test_gates.py
git commit -m "feat(execution): MarginGate admission gate + GateChain begin_batch hook"
```

---

### Task 2: factory 接线（MarginGate 置链尾）

**Files:**
- Modify: `gridtrade/runtime/factory.py`（import + 链尾追加 `MarginGate(adapter, config.default_cap)`）
- Test: `tests/runtime/test_factory.py`（更新 3 门断言为 4 门、末位是 MarginGate）

**Interfaces:**
- Consumes: Task 1 的 `MarginGate(adapter, default_cap)`；factory 内已有 `adapter`（ResilientAdapter）、`config.default_cap`。
- Produces: 无（叶子接线）。

- [ ] **Step 1: 改既有 factory 测试为 4 门（RED）**

把 `tests/runtime/test_factory.py` 的 `test_build_runtime_gate_chain_has_three_gates` 整体替换为：

```python
def test_build_runtime_gate_chain_has_four_gates_margin_last():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.gates import (SymbolLockGate, MaxConcurrentGate,
                                          RiskBudgetGate, MarginGate)
    rt = build_runtime(_cfg())
    gates = rt.manager.gates.gates
    assert len(gates) == 4
    assert isinstance(gates[0], SymbolLockGate)
    assert isinstance(gates[1], MaxConcurrentGate)
    assert isinstance(gates[2], RiskBudgetGate)
    assert isinstance(gates[3], MarginGate)         # MarginGate 在链尾
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_factory.py::test_build_runtime_gate_chain_has_four_gates_margin_last -q`
Expected: FAIL —— 当前链仅 3 门（`len(gates) == 4` 不成立）。

- [ ] **Step 3: factory 追加 MarginGate**

在 `gridtrade/runtime/factory.py`：

import 行（约 13-14 行）`from gridtrade.execution.gates import (GateChain, MaxConcurrentGate, RiskBudgetGate, SymbolLockGate)` 改为加上 `MarginGate`：

```python
from gridtrade.execution.gates import (GateChain, MarginGate, MaxConcurrentGate,
                                       RiskBudgetGate, SymbolLockGate)
```

把 `GateChain([...])` 块改为在末尾追加 MarginGate：

```python
    gates = GateChain([
        SymbolLockGate(executor.grids),
        MaxConcurrentGate(executor.grids, config.max_concurrent),
        RiskBudgetGate(executor.grids, config.total_budget, config.default_cap),
        MarginGate(adapter, config.default_cap),
    ])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_factory.py -q`
Expected: PASS。

- [ ] **Step 5: 跑全套确认无回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add gridtrade/runtime/factory.py tests/runtime/test_factory.py
git commit -m "feat(runtime): wire MarginGate at end of admission gate chain"
```

---

## 自检（Self-Review）

- **Spec 覆盖**：组件与放行语义（cash≥cap、累计扣减、显式 cap）→ Task 1 Step 3b + 单测；begin_batch 钩子 + filter 调用 → Task 1 Step 3a + 链集成测试；fail-closed → Task 1 fail-closed 测试；排序约束（链尾）→ Task 2 接线 + 末位断言；接线无新增 config → Task 2 复用 adapter/default_cap。覆盖完整。
- **占位符**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型/命名一致**：`MarginGate(adapter, default_cap)`、`begin_batch`、`GateResult(passed, gate, reason)`、
  `Balance.cash`、`proposal.cap` 三处一致；factory import 与构造一致；Task 2 替换的测试函数名与断言自洽。
- **既有测试影响**：Task 1 显式说明 factory 3 门测试此阶段仍过；Task 2 才把它改 4 门——顺序自洽，无悬空。
