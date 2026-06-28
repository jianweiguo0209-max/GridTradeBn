# 交易所解耦重构 P4d 实现计划（GridManager 编排器 + 事件总线 Observer）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「触发 → 准入 → 执行」三段接成一条编排链（design.md §6③）：`GridManager` 持有共享 `GridExecutor` + 准入门链 `GateChain` + 事件总线，提供 ① `open_proposals`（提议过门 → `executor.open` → 发 `GridOpened`）② `monitor_all`（逐 ACTIVE 网格跑 `monitor_grid` → 平仓则发 `GridClosed`）。事件总线（Observer）把领域事件与通知/指标解耦。

**Architecture:** 一个 `GridExecutor` 实例按 `grid_id` 管多网格（共享 cap/leverage，对应 legacy 均仓），故 `GridManager` 持单 executor。`open_proposals` 用 `GateChain.filter` 过闸后逐个 `executor.open`，每开一个发 `GridOpened`。`monitor_all` 遍历 `executor.grids.list_active()` 中 `status==ACTIVE` 的网格、对每个调既有 `monitor_grid`，返回 closed 则发 `GridClosed`。`EventBus` 是极简 Observer（subscribe/publish），handler 自行按事件类型过滤。`OrderFilled` 事件需改 executor 内部摄入路径，本增量不做（接口预留，见延后）。

**Tech Stack:** Python 3.9、dataclasses、pytest、FakeExchange + 内存 SQLite。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（编排粒度、监控网格筛选口径、事件字段、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；`gridtrade/execution/` 不得 import 交易所库（GridManager 经注入的 executor/adapter 间接用）。
- 只新增 `gridtrade/execution/events.py`、`gridtrade/execution/manager.py` 及 `tests/execution/test_events.py`、`tests/execution/test_manager.py`；不改 `core/`、`state/`、`exchanges/`、`backtest/`、已有 `execution/*`（仅 import 既有 `gates`/`triggers`/`grid_executor`/`monitor`）。
- 复用既有 `monitor_grid(executor, grid_id, symbol, stop_cfg, *, margin_rate=0.05)`、`GateChain`、`GridProposal`、`GridExecutor`（不改其签名）。
- `monitor_all` 只监控 `status == ACTIVE` 的网格（PENDING/OPENING/CLOSING 为过渡态，不在监控步推进）。
- 编排只用执行器现有内存态：GridManager 假设 executor 已持有对应网格内存态（同进程 open 或 reconciler 重建后）；跨进程重建是 reconciler 职责，不在本增量。
- `OrderFilled` 事件本增量不实现（需改 executor 摄入路径）；EventBus 设计须能容纳未来事件类型（publish 接受任意 dataclass）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 145 passed）。

---

## 文件结构（本计划新建）

```
gridtrade/execution/
  events.py       # 新增：EventBus(Observer) + GridOpened / GridClosed
  manager.py      # 新增：GridManager（open_proposals / monitor_all）
tests/execution/
  test_events.py
  test_manager.py
```

公共接口：

```python
# events.py
@dataclass
class GridOpened:
    grid_id: str; exchange: str; symbol: str; tag: str

@dataclass
class GridClosed:
    grid_id: str; exchange: str; symbol: str; reason: str; pnl_ratio: float

class EventBus:
    def subscribe(self, handler) -> None: ...   # handler(event) -> None
    def publish(self, event) -> None: ...        # 调所有 handler

# manager.py
class GridManager:
    def __init__(self, executor, gate_chain, *, stop_cfg, margin_rate=0.05,
                 event_bus=None): ...
    def open_proposals(self, proposals) -> List[str]: ...   # 返回已开 grid_id
    def monitor_all(self) -> List[dict]: ...                # 每网格 {grid_id, closed, reason, pnl_ratio}
```

---

### Task 1: EventBus + 事件类型（Observer）

**Files:**
- Create: `gridtrade/execution/events.py`
- Create: `tests/execution/test_events.py`

**Interfaces:**
- Produces: `EventBus`（subscribe/publish）、`GridOpened`、`GridClosed`。
- Consumes: 标准库 `dataclasses`。

- [ ] **Step 1: 写失败测试**

Create `tests/execution/test_events.py`:

```python
from gridtrade.execution.events import EventBus, GridOpened, GridClosed


def test_publish_delivers_to_all_subscribers():
    seen_a, seen_b = [], []
    bus = EventBus()
    bus.subscribe(seen_a.append)
    bus.subscribe(seen_b.append)
    ev = GridOpened(grid_id='g1', exchange='okx', symbol='BTC/USDT:USDT', tag='t0')
    bus.publish(ev)
    assert seen_a == [ev] and seen_b == [ev]


def test_handlers_can_filter_by_event_type():
    closes = []
    bus = EventBus()
    bus.subscribe(lambda e: closes.append(e) if isinstance(e, GridClosed) else None)
    bus.publish(GridOpened(grid_id='g1', exchange='okx', symbol='X', tag='t'))
    bus.publish(GridClosed(grid_id='g1', exchange='okx', symbol='X',
                           reason='固定止损', pnl_ratio=-0.04))
    assert len(closes) == 1 and closes[0].reason == '固定止损'


def test_publish_with_no_subscribers_is_noop():
    EventBus().publish(GridOpened(grid_id='g', exchange='e', symbol='s', tag='t'))
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_events.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.events`）。

- [ ] **Step 3: 实现 events.py**

Create `gridtrade/execution/events.py`:

```python
"""领域事件 + 事件总线（Observer）—— 把 GridOpened/GridClosed 与通知/指标解耦。

handler 是 callable(event)；handler 自行按事件类型（isinstance）过滤关心的事件。
publish 接受任意事件 dataclass，便于未来扩展（如 OrderFilled）。
"""
from dataclasses import dataclass
from typing import Callable, List


@dataclass
class GridOpened:
    grid_id: str
    exchange: str
    symbol: str
    tag: str


@dataclass
class GridClosed:
    grid_id: str
    exchange: str
    symbol: str
    reason: str
    pnl_ratio: float


class EventBus:
    def __init__(self):
        self._handlers: List[Callable] = []

    def subscribe(self, handler: Callable) -> None:
        self._handlers.append(handler)

    def publish(self, event) -> None:
        for handler in self._handlers:
            handler(event)
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_events.py -q`
Expected: 3 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/events.py tests/execution/test_events.py
git commit -m "feat(execution): EventBus + GridOpened/GridClosed events (P4d)"
```

---

### Task 2: GridManager.open_proposals（提议过门 → 开仓 → 发事件）

**Files:**
- Create: `gridtrade/execution/manager.py`
- Create: `tests/execution/test_manager.py`

**Interfaces:**
- Consumes: `GateChain.filter(proposals)`、`GridExecutor.open(exchange, symbol, grid_params, *, offset, tag) -> grid_id`、`GridProposal`、`EventBus.publish`、`GridOpened`。
- Produces: `GridManager(executor, gate_chain, *, stop_cfg, margin_rate=0.05, event_bus=None)`，`open_proposals(proposals) -> List[str]`。

- [ ] **Step 1: 写失败测试**

Create `tests/execution/test_manager.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.gates import GridProposal, GateChain, SymbolLockGate
from gridtrade.execution.events import EventBus, GridOpened, GridClosed

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)],
                      price=price)
    ex.set_price(SYM, price)
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    return ex, store, gx


def _proposal(symbol=SYM, exchange='fake'):
    return GridProposal(exchange=exchange, symbol=symbol, grid_params=dict(GP),
                        offset=0, tag='t0', source='test')


def _manager(gx, store, bus=None):
    from gridtrade.execution.manager import GridManager
    chain = GateChain([SymbolLockGate(gx.grids)])
    return GridManager(gx, chain, stop_cfg=STOP_CFG, event_bus=bus)


def test_open_proposals_opens_passing_and_returns_ids():
    ex, store, gx = _setup()
    bus = EventBus(); opened_events = []
    bus.subscribe(lambda e: opened_events.append(e) if isinstance(e, GridOpened) else None)
    mgr = _manager(gx, store, bus)
    ids = mgr.open_proposals([_proposal()])
    assert len(ids) == 1
    assert gx.grids.get(ids[0]).status == 'ACTIVE'
    # 发了 GridOpened 事件，字段正确
    assert len(opened_events) == 1
    assert opened_events[0].grid_id == ids[0] and opened_events[0].symbol == SYM
    assert opened_events[0].tag == 't0'


def test_open_proposals_blocked_by_gate_not_opened():
    ex, store, gx = _setup()
    mgr = _manager(gx, store)
    mgr.open_proposals([_proposal()])           # 先开一个 BTC 活跃网格
    ids2 = mgr.open_proposals([_proposal()])     # 同币种再提议 -> SymbolLockGate 拦
    assert ids2 == []


def test_open_proposals_empty_list_noop():
    ex, store, gx = _setup()
    mgr = _manager(gx, store)
    assert mgr.open_proposals([]) == []
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_manager.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.manager`）。

- [ ] **Step 3: 实现 manager.py（含 open_proposals）**

Create `gridtrade/execution/manager.py`:

```python
"""GridManager —— 组合编排器（design.md §6③）。

持有单个共享 GridExecutor（按 grid_id 管多网格，cap/leverage 共享 = legacy 均仓）、
准入门链 GateChain、可选事件总线。把「触发产出的提议 → 过门 → 开仓 → 发事件」与
「逐 ACTIVE 网格 monitor_grid → 平仓发事件」两段编排起来。
"""
from typing import List

from gridtrade.state.models import ACTIVE
from gridtrade.execution.events import GridOpened, GridClosed
from gridtrade.execution.monitor import monitor_grid


class GridManager:
    def __init__(self, executor, gate_chain, *, stop_cfg, margin_rate=0.05,
                 event_bus=None):
        self.executor = executor
        self.gates = gate_chain
        self.stop_cfg = stop_cfg
        self.margin_rate = float(margin_rate)
        self.bus = event_bus

    def _publish(self, event) -> None:
        if self.bus is not None:
            self.bus.publish(event)

    def open_proposals(self, proposals) -> List[str]:
        opened: List[str] = []
        for proposal in self.gates.filter(proposals):
            gid = self.executor.open(
                proposal.exchange, proposal.symbol, proposal.grid_params,
                offset=proposal.offset, tag=proposal.tag)
            opened.append(gid)
            self._publish(GridOpened(grid_id=gid, exchange=proposal.exchange,
                                     symbol=proposal.symbol, tag=proposal.tag))
        return opened
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_manager.py -q`
Expected: 3 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/manager.py tests/execution/test_manager.py
git commit -m "feat(execution): GridManager.open_proposals (gate -> open -> GridOpened) (P4d)"
```

---

### Task 3: GridManager.monitor_all（逐 ACTIVE 网格推进 → 平仓发事件）

**Files:**
- Modify: `gridtrade/execution/manager.py`
- Modify: `tests/execution/test_manager.py`

**Interfaces:**
- Consumes: `GridExecutor.grids.list_active() -> List[Grid]`、`monitor_grid(executor, grid_id, symbol, stop_cfg, *, margin_rate)`、`GridClosed`、`gridtrade.state.models.ACTIVE`。
- Produces: `GridManager.monitor_all() -> List[dict]`（每项 `{grid_id, closed, reason, pnl_ratio}`）。

- [ ] **Step 1: 写失败测试**

在 `tests/execution/test_manager.py` 末尾追加：

```python
def test_monitor_all_no_exit_returns_open_results():
    ex, store, gx = _setup(100.0)
    mgr = _manager(gx, store)
    mgr.open_proposals([_proposal()])
    res = mgr.monitor_all()
    assert len(res) == 1
    assert res[0]['closed'] is False and res[0]['reason'] is None


def test_monitor_all_triggers_stop_and_publishes_grid_closed():
    ex, store, gx = _setup(100.0)
    bus = EventBus(); closed_events = []
    bus.subscribe(lambda e: closed_events.append(e) if isinstance(e, GridClosed) else None)
    mgr = _manager(gx, store, bus)
    ids = mgr.open_proposals([_proposal()])
    ex.set_price(SYM, 96.5)   # 大跌触发固定止损
    res = mgr.monitor_all()
    assert res[0]['closed'] is True and res[0]['reason'] == '固定止损'
    assert gx.grids.get(ids[0]).status == 'CLOSED'
    # 发了 GridClosed 事件
    assert len(closed_events) == 1
    assert closed_events[0].grid_id == ids[0] and closed_events[0].reason == '固定止损'


def test_monitor_all_no_active_grids_returns_empty():
    ex, store, gx = _setup()
    mgr = _manager(gx, store)
    assert mgr.monitor_all() == []
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_manager.py -k monitor_all -q`
Expected: FAIL（`AttributeError: 'GridManager' object has no attribute 'monitor_all'`）。

- [ ] **Step 3: 实现 monitor_all**

在 `gridtrade/execution/manager.py` 的 `GridManager` 类末尾追加方法：

```python
    def monitor_all(self) -> List[dict]:
        results: List[dict] = []
        # 取快照列表，只推进 ACTIVE 网格（PENDING/OPENING/CLOSING 为过渡态）
        active = [g for g in self.executor.grids.list_active()
                  if g.status == ACTIVE]
        for grid in active:
            res = monitor_grid(self.executor, grid.id, grid.symbol,
                               self.stop_cfg, margin_rate=self.margin_rate)
            if res['closed']:
                self._publish(GridClosed(
                    grid_id=grid.id, exchange=grid.exchange, symbol=grid.symbol,
                    reason=res['reason'], pnl_ratio=res['pnl_ratio']))
            results.append({'grid_id': grid.id, **res})
        return results
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_manager.py -q`
Expected: 全 PASS（6）。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 145 + 新增 events/manager 测试）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/manager.py tests/execution/test_manager.py
git commit -m "feat(execution): GridManager.monitor_all (monitor_grid -> GridClosed) (P4d)"
```

---

## Self-Review

- **Spec 覆盖**：design.md §6③ GridManager（持 executor、逐网格独立推进）—— `open_proposals`（Task 2）+ `monitor_all`（Task 3）；Observer 事件总线（§5/§10 解耦）—— `EventBus` + `GridOpened/GridClosed`（Task 1）。`OrderFilled` 显式延后（需改 executor 摄入路径），EventBus 已能容纳。
- **复用**：`GateChain`（P4b）、`GridProposal`（P4b）、`monitor_grid`（P3d）、`GridExecutor`（P3c）全部复用，零改签名。
- **粒度决策记录**：单 GridExecutor 按 grid_id 管多网格（共享 cap/leverage = legacy 均仓）；`monitor_all` 只推进 `status==ACTIVE` 网格。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`GridManager` 构造参数（executor/gate_chain/stop_cfg/margin_rate/event_bus）与测试一致；事件字段（GridOpened: grid_id/exchange/symbol/tag；GridClosed: +reason/pnl_ratio）在 events.py 与 manager 发布处一致；`monitor_all` 返回项键（grid_id/closed/reason/pnl_ratio）与 `monitor_grid` 输出对齐。
