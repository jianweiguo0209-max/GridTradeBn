# 交易所解耦重构 P4e 实现计划（运行时循环编排：scheduler 周期 + monitor 周期 + 重启自愈）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 P4a–d 的组件接成两个**纯循环函数**（design.md §8 两个 Fly Machine 角色的循环体），全程离线可测：① `run_scheduler_cycle`（按 tag 关旧网格 → 触发 → 准入 → 开仓，复刻 legacy 主流程顺序）② `run_monitor_cycle`（逐 ACTIVE 网格对账补单 → `monitor_all` 止盈止损）+ `restore_all`（重启自愈：为 DB 中 ACTIVE 网格重建执行器内存态）。新增 `GridManager.close_by_tag`（按 offset tag 关旧）。`while/sleep` 守护进程、信号处理、心跳、具体 config/币池/DataSource 接线属部署耦合，留 P4-deploy。

**Architecture:** 循环函数是**数据源无关**的纯编排：candle 数据由调用方放进 `TriggerContext.symbol_candle_data`（DataSource 预热缓存或实时 adapter 均可，循环不关心）。scheduler 机 scale-to-zero（每次唤醒是全新进程），故关旧网格前必须先 `Reconciler.restore` 重建其内存态（否则 `executor.close` 取不到 `_geom/live` 而 KeyError）。monitor 机常驻，循环体 = 对账补单 + `monitor_all`；进程重启时先 `restore_all` 收敛再进循环。

**Tech Stack:** Python 3.9、pytest、FakeExchange + 内存 SQLite。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（关旧口径、对账顺序、重建时机、reason 标签、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；新增 `gridtrade/runtime/__init__.py`、`gridtrade/runtime/cycles.py` 及 `tests/runtime/test_cycles.py`、`tests/runtime/__init__.py`（如测试目录需要）；改 `gridtrade/execution/manager.py`（加 `close_by_tag`）+ `tests/execution/test_manager.py`。不改 `core/`、`state/`、`exchanges/`、`backtest/`、已有 `execution/{grid_executor,reconciler,monitor,gates,triggers,events,live_equity}.py`。
- 复用既有：`GridManager`（P4d）、`Reconciler.restore/reconcile_open_orders`（P3d）、`monitor_grid`（P3d）、`TriggerEngine.collect`（P4c）、`GridExecutor.close`（P3c，返回 `{'reason','pnl_ratio'}`）。不改其签名。
- 关旧网格口径同 legacy `close_grid(tag=offset_order_tag)`：关 `status==ACTIVE` 且 `grid.tag == close_tag` 的网格。rebalance 关仓 reason 标签 = `'周期再平衡'`（仅为 records.exit_reason 标签，不影响行为；可由调用方覆盖）。
- scheduler 周期顺序复刻 legacy main：**先关旧（restore→close）再选币开新**。
- monitor 周期顺序：**先对账补单（reconcile_open_orders）再 monitor_all（sync+evaluate_exit→close）**。
- 循环只推进 `status==ACTIVE` 网格（list_active 含 PENDING/OPENING/CLOSING 过渡态，须过滤）。
- 循环函数纯编排、不 sleep、不 import 交易所库、不读全局 config（全部依赖注入）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 154 passed）。

---

## 文件结构（本计划新建/修改）

```
gridtrade/execution/
  manager.py      # 修改：+ GridManager.close_by_tag(tag, reason) -> List[str]
gridtrade/runtime/
  __init__.py     # 新增（空包）
  cycles.py       # 新增：restore_all / run_monitor_cycle / run_scheduler_cycle
tests/runtime/
  __init__.py     # 新增（如需）
  test_cycles.py
tests/execution/
  test_manager.py # 修改：+ close_by_tag 测试
```

公共接口：

```python
# manager.py
class GridManager:
    def close_by_tag(self, tag: str, reason: str) -> List[str]: ...  # 关旧网格，返回已关 id

# runtime/cycles.py
def restore_all(reconciler) -> List[str]: ...
    # 为所有 status==ACTIVE 网格 Reconciler.restore，返回重建的 grid_id 列表
def run_monitor_cycle(reconciler, manager) -> dict: ...
    # {'reconciled': {grid_id: {canceled,replaced}}, 'monitored': [...]}
def run_scheduler_cycle(manager, trigger_engine, reconciler, ctx, *,
                        close_tag=None, close_reason='周期再平衡') -> dict: ...
    # {'closed': [grid_id...], 'opened': [grid_id...]}
```

---

### Task 1: GridManager.close_by_tag（按 offset tag 关旧网格）

**Files:**
- Modify: `gridtrade/execution/manager.py`
- Modify: `tests/execution/test_manager.py`

**Interfaces:**
- Consumes: `GridExecutor.grids.list_active()`、`GridExecutor.close(grid_id, symbol, reason) -> {'reason','pnl_ratio'}`、`GridClosed`、`gridtrade.state.models.ACTIVE`。
- Produces: `GridManager.close_by_tag(tag, reason) -> List[str]`（关 ACTIVE 且 tag 匹配的网格，发 GridClosed，返回 id 列表）。

- [ ] **Step 1: 写失败测试**

在 `tests/execution/test_manager.py` 末尾追加：

```python
def test_close_by_tag_closes_matching_active_grids_and_publishes():
    ex, store, gx = _setup(100.0)
    bus = EventBus(); closed_events = []
    bus.subscribe(lambda e: closed_events.append(e) if isinstance(e, GridClosed) else None)
    mgr = _manager(gx, store, bus)
    ids = mgr.open_proposals([_proposal()])          # tag='t0'
    out = mgr.close_by_tag('t0', '周期再平衡')
    assert out == ids
    assert gx.grids.get(ids[0]).status == 'CLOSED'
    assert len(closed_events) == 1
    assert closed_events[0].grid_id == ids[0] and closed_events[0].reason == '周期再平衡'


def test_close_by_tag_ignores_non_matching_tag():
    ex, store, gx = _setup(100.0)
    mgr = _manager(gx, store)
    ids = mgr.open_proposals([_proposal()])          # tag='t0'
    out = mgr.close_by_tag('t999', '周期再平衡')      # 无匹配
    assert out == []
    assert gx.grids.get(ids[0]).status == 'ACTIVE'   # 未动
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_manager.py -k close_by_tag -q`
Expected: FAIL（`AttributeError: 'GridManager' object has no attribute 'close_by_tag'`）。

- [ ] **Step 3: 实现 close_by_tag**

在 `gridtrade/execution/manager.py` 的 `GridManager` 类末尾追加：

```python
    def close_by_tag(self, tag: str, reason: str) -> List[str]:
        closed: List[str] = []
        active = [g for g in self.executor.grids.list_active()
                  if g.status == ACTIVE and g.tag == tag]
        for grid in active:
            res = self.executor.close(grid.id, grid.symbol, reason)
            self._publish(GridClosed(
                grid_id=grid.id, exchange=grid.exchange, symbol=grid.symbol,
                reason=reason, pnl_ratio=res['pnl_ratio']))
            closed.append(grid.id)
        return closed
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_manager.py -q`
Expected: 全 PASS（8）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/manager.py tests/execution/test_manager.py
git commit -m "feat(execution): GridManager.close_by_tag for offset rebalance close (P4e)"
```

---

### Task 2: restore_all + run_monitor_cycle（monitor 机循环体 + 重启自愈）

**Files:**
- Create: `gridtrade/runtime/__init__.py`
- Create: `gridtrade/runtime/cycles.py`
- Create: `tests/runtime/__init__.py`
- Create: `tests/runtime/test_cycles.py`

**Interfaces:**
- Consumes: `Reconciler.restore(grid_id)`、`Reconciler.reconcile_open_orders(grid_id, symbol) -> {canceled,replaced}`、`reconciler.ex.grids.list_active()`、`GridManager.monitor_all()`、`gridtrade.state.models.ACTIVE`。
- Produces: `restore_all(reconciler) -> List[str]`；`run_monitor_cycle(reconciler, manager) -> dict`。

- [ ] **Step 1: 写失败测试**

Create `gridtrade/runtime/__init__.py`（空文件）。
Create `tests/runtime/__init__.py`（空文件，如 pytest 需要）。
Create `tests/runtime/test_cycles.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.gates import GridProposal, GateChain, SymbolLockGate
from gridtrade.execution.manager import GridManager
from gridtrade.execution.triggers import TriggerCondition, TriggerEngine, TriggerContext

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(price=100.0):
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=price)
    ex.set_price(BTC, price); ex.set_price(ETH, price)
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    chain = GateChain([SymbolLockGate(gx.grids)])
    mgr = GridManager(gx, chain, stop_cfg=STOP_CFG)
    return ex, store, gx, mgr


def _proposal(symbol=BTC, tag='t0'):
    return GridProposal(exchange='fake', symbol=symbol, grid_params=dict(GP),
                        offset=0, tag=tag, source='test')


def test_run_monitor_cycle_reconciles_then_monitors_no_exit():
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(100.0)
    ids = mgr.open_proposals([_proposal()])
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert set(out['reconciled'].keys()) == set(ids)
    assert out['reconciled'][ids[0]] == {'canceled': 0, 'replaced': 0}
    assert out['monitored'][0]['closed'] is False


def test_run_monitor_cycle_triggers_stop_close():
    from gridtrade.runtime.cycles import run_monitor_cycle
    ex, store, gx, mgr = _setup(100.0)
    ids = mgr.open_proposals([_proposal()])
    ex.set_price(BTC, 96.5)
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert out['monitored'][0]['closed'] is True
    assert gx.grids.get(ids[0]).status == 'CLOSED'


def test_restore_all_rebuilds_memory_then_monitor_works():
    from gridtrade.runtime.cycles import restore_all, run_monitor_cycle
    ex, store, gx, mgr = _setup(100.0)
    ids = mgr.open_proposals([_proposal()])
    # 模拟「全新进程」：清空执行器内存态
    gx._geom.clear(); gx.live.clear(); gx._seq.clear()
    gx._trade_cursor.clear(); gx._funding_cursor.clear()
    restored = restore_all(Reconciler(gx))
    assert restored == ids
    # 重建后 monitor 周期不再 KeyError
    out = run_monitor_cycle(Reconciler(gx), mgr)
    assert out['monitored'][0]['closed'] is False


def test_restore_all_empty_when_no_active():
    from gridtrade.runtime.cycles import restore_all
    ex, store, gx, mgr = _setup()
    assert restore_all(Reconciler(gx)) == []
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_cycles.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.runtime.cycles`）。

- [ ] **Step 3: 实现 cycles.py（restore_all + run_monitor_cycle）**

Create `gridtrade/runtime/cycles.py`:

```python
"""运行时循环体（design.md §8 两个 Fly Machine 角色的循环编排）。

数据源无关的纯编排：candle 数据由调用方放进 TriggerContext.symbol_candle_data。
while/sleep 守护进程、信号、心跳、config/币池/DataSource 接线属部署耦合（P4-deploy）。
"""
from typing import List

from gridtrade.state.models import ACTIVE


def _active_grids(grids_repo):
    return [g for g in grids_repo.list_active() if g.status == ACTIVE]


def restore_all(reconciler) -> List[str]:
    """重启自愈：为 DB 中所有 ACTIVE 网格重建执行器内存态。"""
    restored: List[str] = []
    for grid in _active_grids(reconciler.ex.grids):
        reconciler.restore(grid.id)
        restored.append(grid.id)
    return restored


def run_monitor_cycle(reconciler, manager) -> dict:
    """monitor 机循环体：先逐网格对账补单，再 monitor_all 止盈止损。"""
    reconciled = {}
    for grid in _active_grids(manager.executor.grids):
        reconciled[grid.id] = reconciler.reconcile_open_orders(grid.id, grid.symbol)
    monitored = manager.monitor_all()
    return {'reconciled': reconciled, 'monitored': monitored}
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_cycles.py -q`
Expected: 4 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/runtime/__init__.py gridtrade/runtime/cycles.py tests/runtime/
git commit -m "feat(runtime): restore_all + run_monitor_cycle (monitor machine loop body) (P4e)"
```

---

### Task 3: run_scheduler_cycle（关旧 → 触发 → 准入 → 开仓）

**Files:**
- Modify: `gridtrade/runtime/cycles.py`
- Modify: `tests/runtime/test_cycles.py`

**Interfaces:**
- Consumes: `Reconciler.restore`、`GridManager.close_by_tag`、`GridManager.open_proposals`、`TriggerEngine.collect(ctx)`、`TriggerContext`。
- Produces: `run_scheduler_cycle(manager, trigger_engine, reconciler, ctx, *, close_tag=None, close_reason='周期再平衡') -> dict`。

- [ ] **Step 1: 写失败测试**

在 `tests/runtime/test_cycles.py` 末尾追加：

```python
class _FixedTrigger(TriggerCondition):
    def __init__(self, props):
        self._props = props
    def propose(self, ctx):
        return list(self._props)


def test_run_scheduler_cycle_closes_old_tag_then_opens_new():
    from gridtrade.runtime.cycles import run_scheduler_cycle
    import pandas as pd
    ex, store, gx, mgr = _setup(100.0)
    old = mgr.open_proposals([_proposal(symbol=BTC, tag='t0')])   # 旧 BTC 网格 tag=t0
    engine = TriggerEngine([_FixedTrigger([_proposal(symbol=ETH, tag='t0')])])
    ctx = TriggerContext(exchange='fake', run_time=pd.Timestamp('2025-06-24 14:00:00'))
    out = run_scheduler_cycle(mgr, engine, Reconciler(gx), ctx, close_tag='t0')
    assert out['closed'] == old
    assert gx.grids.get(old[0]).status == 'CLOSED'
    assert len(out['opened']) == 1
    assert gx.grids.get(out['opened'][0]).symbol == ETH
    assert gx.grids.get(out['opened'][0]).status == 'ACTIVE'


def test_run_scheduler_cycle_restore_before_close_in_fresh_process():
    from gridtrade.runtime.cycles import run_scheduler_cycle
    import pandas as pd
    ex, store, gx, mgr = _setup(100.0)
    old = mgr.open_proposals([_proposal(symbol=BTC, tag='t0')])
    # 模拟 scheduler scale-to-zero 全新进程：清空内存态
    gx._geom.clear(); gx.live.clear(); gx._seq.clear()
    gx._trade_cursor.clear(); gx._funding_cursor.clear()
    engine = TriggerEngine([])   # 不开新，只验证关旧前 restore 不 KeyError
    ctx = TriggerContext(exchange='fake', run_time=pd.Timestamp('2025-06-24 14:00:00'))
    out = run_scheduler_cycle(mgr, engine, Reconciler(gx), ctx, close_tag='t0')
    assert out['closed'] == old
    assert gx.grids.get(old[0]).status == 'CLOSED'


def test_run_scheduler_cycle_no_close_tag_only_opens():
    from gridtrade.runtime.cycles import run_scheduler_cycle
    import pandas as pd
    ex, store, gx, mgr = _setup(100.0)
    engine = TriggerEngine([_FixedTrigger([_proposal(symbol=BTC, tag='t0')])])
    ctx = TriggerContext(exchange='fake', run_time=pd.Timestamp('2025-06-24 14:00:00'))
    out = run_scheduler_cycle(mgr, engine, Reconciler(gx), ctx)
    assert out['closed'] == []
    assert len(out['opened']) == 1
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_cycles.py -k scheduler -q`
Expected: FAIL（`ImportError: cannot import name 'run_scheduler_cycle'`）。

- [ ] **Step 3: 实现 run_scheduler_cycle**

在 `gridtrade/runtime/cycles.py` 末尾追加：

```python
def run_scheduler_cycle(manager, trigger_engine, reconciler, ctx, *,
                        close_tag=None, close_reason='周期再平衡') -> dict:
    """scheduler 机循环体（复刻 legacy 主流程顺序）：先关旧 tag 网格、再触发→准入→开仓。

    scheduler 机 scale-to-zero（全新进程），关旧前先 Reconciler.restore 重建内存态，
    否则 executor.close 取不到 _geom/live。
    """
    closed: List[str] = []
    if close_tag is not None:
        to_close = [g for g in _active_grids(manager.executor.grids)
                    if g.tag == close_tag]
        for grid in to_close:
            reconciler.restore(grid.id)   # 全新进程：先重建内存态
        closed = manager.close_by_tag(close_tag, close_reason)
    proposals = trigger_engine.collect(ctx)
    opened = manager.open_proposals(proposals)
    return {'closed': closed, 'opened': opened}
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_cycles.py -q`
Expected: 全 PASS（7）。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 154 + 新增 close_by_tag/cycles 测试）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/runtime/cycles.py tests/runtime/test_cycles.py
git commit -m "feat(runtime): run_scheduler_cycle (close-old -> trigger -> gate -> open) (P4e)"
```

---

## Self-Review

- **Spec 覆盖**：design.md §8 两个角色循环体 —— monitor 机（`run_monitor_cycle` = 对账补单 + monitor_all）+ 重启自愈（`restore_all`）（Task 2）；scheduler 机（`run_scheduler_cycle` = 关旧→选币→准入→开仓，主流程延用）（Task 3）；关旧网格 `GridManager.close_by_tag`（Task 1，对应 legacy close_grid by tag）。
- **部署耦合显式留 P4-deploy**：`while/sleep` 守护循环、信号/优雅停、心跳/健康检查、降级不 sys.exit 的交易所调用包装、具体 config/币池/DataSource 接线、fly.toml 多 process group。
- **scale-to-zero 正确性**：scheduler 关旧前 `Reconciler.restore` 重建内存态（test_run_scheduler_cycle_restore_before_close_in_fresh_process 覆盖全新进程路径）。
- **复用**：close（P3c）、Reconciler（P3d）、monitor_grid（P3d）、GridManager（P4d）、TriggerEngine（P4c）全部复用，零改签名。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`run_monitor_cycle` 返回 `{'reconciled': {id: {canceled,replaced}}, 'monitored': [...]}`；`run_scheduler_cycle` 返回 `{'closed': [...], 'opened': [...]}`；`close_by_tag(tag, reason) -> List[str]` 与调用处一致；`_active_grids` 过滤 `status==ACTIVE`。
