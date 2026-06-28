# 交易所解耦重构 P4k 实现计划（HL testnet 支持 + build_runtime 组装工厂）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ① 给 `HyperliquidAdapter.from_credentials` 加 `testnet` 支持（ccxt `set_sandbox_mode(True)` → api.hyperliquid-testnet.xyz，先 testnet 上线用），`build_adapter` 透传；② `build_runtime(config)` 工厂：从 `DeployConfig` 一站组装全部运行时组件（adapter→ResilientAdapter→StateStore+create_all→GridExecutor→GateChain[三门]→GridManager→TriggerEngine[ScheduledSelectionTrigger]→Reconciler→HeartbeatRepository→EventBus），返回 `Runtime` bundle。守护进程 P4l 直接吃 bundle。

**Architecture:** 工厂用既有 `build_adapter(dict)`（registry）构内层 adapter，再包 `ResilientAdapter`（带共享 CircuitBreaker）。state 用 `StateStore.from_url(database_url)`（空则 in_memory，便于离线测试）+ `create_all()`。门链 = SymbolLock+MaxConcurrent+RiskBudget（用 config 的 max_concurrent/total_budget/default_cap）。触发器用 `DEFAULT_STRATEGY_CONFIG`。全部依赖注入，工厂用 `exchange='fake'` 离线可测。

**Tech Stack:** Python 3.9、ccxt（仅 from_credentials）、SQLAlchemy、pytest、FakeExchange + 内存 SQLite。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（testnet 口径、组装参数、bundle 字段、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；改 `gridtrade/exchanges/hyperliquid.py`（from_credentials +testnet）、`gridtrade/exchanges/registry.py`（build_adapter 透传 testnet）；新增 `gridtrade/runtime/factory.py` 及测试。不改 core/state/backtest/已有 execution 逻辑。
- testnet：`from_credentials(..., testnet=False)`；`testnet=True` 时 `client.set_sandbox_mode(True)`（构造期，无网络）。
- `build_runtime` 的 state：`database_url` 非空 → `StateStore.from_url(url)`，空 → `StateStore.in_memory()`；都 `create_all()`。
- 门链顺序：`[SymbolLockGate, MaxConcurrentGate, RiskBudgetGate]`（先互斥后并发后预算）。
- 触发器 strategy_config 用 `gridtrade.config.DEFAULT_STRATEGY_CONFIG`；stop_cfg 用 `DEFAULT_STOP_CFG`。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 203 passed）。

---

## 文件结构（本计划新建/修改）

```
gridtrade/exchanges/hyperliquid.py   # 修改：from_credentials(..., testnet=False)
gridtrade/exchanges/registry.py      # 修改：build_adapter 透传 testnet（hyperliquid 分支）
gridtrade/runtime/factory.py         # 新增：Runtime dataclass + build_runtime(config)
tests/exchanges/test_hl_testnet.py   # 新增
tests/runtime/test_factory.py        # 新增
```

公共接口：

```python
@dataclass
class Runtime:
    config: object            # DeployConfig
    adapter: object           # ResilientAdapter
    store: object             # StateStore
    executor: object          # GridExecutor
    manager: object           # GridManager
    trigger_engine: object    # TriggerEngine
    reconciler: object        # Reconciler
    heartbeats: object        # HeartbeatRepository
    event_bus: object         # EventBus

def build_runtime(config) -> Runtime: ...
```

---

### Task 1: HyperliquidAdapter testnet 支持 + build_adapter 透传

**Files:**
- Modify: `gridtrade/exchanges/hyperliquid.py`
- Modify: `gridtrade/exchanges/registry.py`
- Create: `tests/exchanges/test_hl_testnet.py`

**Interfaces:**
- Produces: `HyperliquidAdapter.from_credentials(wallet_address, private_key, *, proxies=None, testnet=False)`；`build_adapter` 对 hyperliquid 透传 `config.get('testnet')`。

- [ ] **Step 1: 写失败测试**

Create `tests/exchanges/test_hl_testnet.py`:

```python
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
from gridtrade.exchanges.registry import build_adapter


def test_testnet_uses_sandbox_url():
    ad = HyperliquidAdapter.from_credentials('0xabc', 'key', testnet=True)
    assert 'testnet' in ad.client.urls['api']['public']


def test_mainnet_default_no_sandbox():
    ad = HyperliquidAdapter.from_credentials('0xabc', 'key')
    assert 'testnet' not in ad.client.urls['api']['public']


def test_build_adapter_passes_testnet():
    ad = build_adapter({'exchange': 'hyperliquid', 'wallet_address': '0xabc',
                        'private_key': 'key', 'testnet': True})
    assert 'testnet' in ad.client.urls['api']['public']
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_hl_testnet.py -q`
Expected: FAIL（`from_credentials() got an unexpected keyword argument 'testnet'`）。

- [ ] **Step 3: 实现**

`gridtrade/exchanges/hyperliquid.py` 改 `from_credentials`：

```python
    @classmethod
    def from_credentials(cls, wallet_address, private_key, *, proxies=None,
                         testnet=False):
        import ccxt
        client = ccxt.hyperliquid({
            'walletAddress': wallet_address,
            'privateKey': private_key,
            'enableRateLimit': True,
            'proxies': proxies or {},
        })
        if testnet:
            client.set_sandbox_mode(True)
        return cls(client)
```

`gridtrade/exchanges/registry.py` 改 hyperliquid 分支：

```python
    if name == 'hyperliquid':
        return HyperliquidAdapter.from_credentials(
            config.get('wallet_address', ''), config.get('private_key', ''),
            proxies=config.get('proxies'),
            testnet=bool(config.get('testnet', False)))
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_hl_testnet.py -q`
Expected: 3 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/hyperliquid.py gridtrade/exchanges/registry.py tests/exchanges/test_hl_testnet.py
git commit -m "feat(exchanges): HyperliquidAdapter testnet (sandbox) support (P4k)"
```

---

### Task 2: build_runtime 组装工厂 + Runtime bundle

**Files:**
- Create: `gridtrade/runtime/factory.py`
- Create: `tests/runtime/test_factory.py`

**Interfaces:**
- Consumes: `gridtrade.config.{DeployConfig, load_deploy_config, DEFAULT_STRATEGY_CONFIG, DEFAULT_STOP_CFG}`、`build_adapter`、`ResilientAdapter`、`CircuitBreaker`、`StateStore`、`GridExecutor`、`GateChain`/`SymbolLockGate`/`MaxConcurrentGate`/`RiskBudgetGate`、`GridManager`、`EventBus`、`TriggerEngine`/`ScheduledSelectionTrigger`、`Reconciler`、`HeartbeatRepository`。
- Produces: `Runtime`（dataclass）、`build_runtime(config) -> Runtime`。

- [ ] **Step 1: 写失败测试**

Create `tests/runtime/test_factory.py`:

```python
from gridtrade.config import load_deploy_config


def _cfg(**kw):
    env = {'EXCHANGE': 'fake'}      # fake -> 无需凭证；database_url 空 -> in_memory
    env.update(kw)
    return load_deploy_config(env=env)


def test_build_runtime_wires_all_components():
    from gridtrade.runtime.factory import build_runtime, Runtime
    from gridtrade.exchanges.resilient_adapter import ResilientAdapter
    from gridtrade.execution.grid_executor import GridExecutor
    from gridtrade.execution.manager import GridManager
    from gridtrade.execution.triggers import TriggerEngine
    from gridtrade.execution.reconciler import Reconciler
    from gridtrade.state.heartbeats import HeartbeatRepository

    rt = build_runtime(_cfg(CAP='500', LEVERAGE='4', MAX_CONCURRENT='7'))
    assert isinstance(rt, Runtime)
    assert isinstance(rt.adapter, ResilientAdapter)
    assert isinstance(rt.executor, GridExecutor)
    assert isinstance(rt.manager, GridManager)
    assert isinstance(rt.trigger_engine, TriggerEngine)
    assert isinstance(rt.reconciler, Reconciler)
    assert isinstance(rt.heartbeats, HeartbeatRepository)
    # config 透传到执行器
    assert rt.executor.cap == 500.0 and rt.executor.leverage == 4.0


def test_build_runtime_gate_chain_has_three_gates():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.gates import (SymbolLockGate, MaxConcurrentGate,
                                          RiskBudgetGate)
    rt = build_runtime(_cfg())
    gates = rt.manager.gates.gates
    assert len(gates) == 3
    assert isinstance(gates[0], SymbolLockGate)
    assert isinstance(gates[1], MaxConcurrentGate)
    assert isinstance(gates[2], RiskBudgetGate)


def test_build_runtime_creates_tables_and_trigger_uses_engine():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    rt = build_runtime(_cfg())
    # create_all 已建表：list_active 不报错（空库返回 []）
    assert rt.executor.grids.list_active() == []
    # 触发引擎装了 ScheduledSelectionTrigger
    assert any(isinstance(t, ScheduledSelectionTrigger)
               for t in rt.trigger_engine.triggers)


def test_build_runtime_manager_shares_executor_and_bus_wired():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.events import EventBus
    rt = build_runtime(_cfg())
    assert rt.manager.executor is rt.executor
    assert isinstance(rt.event_bus, EventBus)
    assert rt.manager.bus is rt.event_bus
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_factory.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.runtime.factory`）。

- [ ] **Step 3: 实现 factory.py**

Create `gridtrade/runtime/factory.py`:

```python
"""build_runtime：从 DeployConfig 一站组装全部运行时组件，返回 Runtime bundle。

守护进程（scheduler/monitor）直接吃 bundle。exchange='fake' + 空 database_url 时
全离线可测。
"""
from dataclasses import dataclass

from gridtrade.config import DEFAULT_STOP_CFG, DEFAULT_STRATEGY_CONFIG
from gridtrade.exchanges.registry import build_adapter
from gridtrade.exchanges.resilience import CircuitBreaker
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.events import EventBus
from gridtrade.execution.gates import (GateChain, MaxConcurrentGate,
                                       RiskBudgetGate, SymbolLockGate)
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.manager import GridManager
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.triggers import (ScheduledSelectionTrigger,
                                          TriggerEngine)
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.state.store import StateStore


@dataclass
class Runtime:
    config: object
    adapter: object
    store: object
    executor: object
    manager: object
    trigger_engine: object
    reconciler: object
    heartbeats: object
    event_bus: object


def build_runtime(config) -> Runtime:
    inner = build_adapter({
        'exchange': config.exchange,
        'wallet_address': config.wallet_address,
        'private_key': config.private_key,
        'testnet': config.testnet,
    })
    adapter = ResilientAdapter(inner, breaker=CircuitBreaker())

    store = (StateStore.from_url(config.database_url) if config.database_url
             else StateStore.in_memory())
    store.create_all()

    executor = GridExecutor(adapter, store, cap=config.cap,
                            leverage=config.leverage)
    gates = GateChain([
        SymbolLockGate(executor.grids),
        MaxConcurrentGate(executor.grids, config.max_concurrent),
        RiskBudgetGate(executor.grids, config.total_budget, config.default_cap),
    ])
    bus = EventBus()
    manager = GridManager(executor, gates, stop_cfg=DEFAULT_STOP_CFG,
                          event_bus=bus)

    sc = DEFAULT_STRATEGY_CONFIG
    trigger = ScheduledSelectionTrigger(sc, sc['factors'], sc['weight_list'],
                                        utc_offset=config.utc_offset)
    trigger_engine = TriggerEngine([trigger])

    return Runtime(
        config=config, adapter=adapter, store=store, executor=executor,
        manager=manager, trigger_engine=trigger_engine,
        reconciler=Reconciler(executor),
        heartbeats=HeartbeatRepository(store), event_bus=bus,
    )
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_factory.py -q`
Expected: 4 PASS。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 203 + 新增）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/runtime/factory.py tests/runtime/test_factory.py
git commit -m "feat(runtime): build_runtime factory assembles all components from config (P4k)"
```

---

## Self-Review

- **决策对齐**：testnet 先行（from_credentials testnet=True → sandbox）；HL 首发（build_adapter hyperliquid 分支）；门链三门用 config 风控参数；策略默认常量。
- **Spec 覆盖**：design.md §2 config 统一构造 + §8 运行时组装（adapter/state/strategy 依赖注入）。
- **可测性**：exchange='fake' + 空 db → 全离线；工厂只组装不联网，测试断言组件类型/参数透传/门数。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`build_runtime(config) -> Runtime`；`Runtime` 字段与测试断言一致；`GridExecutor(cap/leverage)`、`GateChain([3 门])`、`GridManager(executor, gates, stop_cfg, event_bus)`、`ScheduledSelectionTrigger(sc, factors, weight_list, utc_offset)` 均与既有签名一致。
