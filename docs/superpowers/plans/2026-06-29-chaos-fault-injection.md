# P6① 故障注入 / 混沌测试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用注入式故障穿过完整执行栈，主动验证执行器+对账+止损在「超时/拒单/限频/维护/部分成交」下守住端到端不变量（无重复单/无孤儿单/记账不漂移/最终收敛/不崩）。

**Architecture:** 新增透明包装适配器 `FaultyAdapter`，按「方法名+第N次调用」消费故障脚本；测试栈 `GridExecutor → ResilientAdapter(快进退避) → FaultyAdapter → FakeExchange + 内存SQLite`，与生产栈同构。混沌测试先取无故障基线，再注入故障，断言收敛回基线。

**Tech Stack:** Python 3.9 / pytest / ccxt 4.5.61 异常类型 / SQLAlchemy 2.0（内存 SQLite）。

## Global Constraints

- 全程**离线、无网络**：仅 FakeExchange + `StateStore.in_memory()`。
- **确定性**：ResilientAdapter 注入 `sleep=lambda _: None` 与 `rng=random.Random(0)`，禁止真 sleep / 真随机。
- `gridtrade/core/` 与 `gridtrade/execution/` `gridtrade/runtime/` **不得 import ccxt**；ccxt 只允许出现在 `gridtrade/exchanges/`。
- 故障脚本元素就是「ccxt 异常实例 / `None`(透传) / `Partial` / `RaiseAfter`」，不发明新异常体系。
- 跑测试命令：`TZ=Asia/Shanghai .venv/bin/python -m pytest`。
- 现有 258 测试必须保持全绿；新增测试并入同一套。
- 生产代码（FakeExchange / runtime）改动一律 TDD（红→绿），且在任务评审检查点向用户汇报"发现 X，建议修/记"。

---

### Task 1: FaultyAdapter 包装适配器 + 单元测试

**Files:**
- Create: `gridtrade/exchanges/faulty.py`
- Test: `tests/exchanges/test_faulty.py`

**Interfaces:**
- Consumes: `gridtrade.exchanges.fake.FakeExchange`、`gridtrade.exchanges.base.Instrument`、ccxt 异常。
- Produces:
  - `FaultyAdapter(inner, schedule: dict[str, list] | None = None)` —— 透明包装任意 ExchangeAdapter（鸭子类型，不继承 ABC）。`schedule` 形如 `{'create_limit_order': [ccxt.RequestTimeout('x'), None]}`，每次调用该方法消费列表头一个故障；耗尽后透传内层。
  - `Partial(ratio: float)` —— dataclass，仅对 `create_market_order` 生效：内层 size×ratio，返回内层结果（filled=ratio×size）。
  - `RaiseAfter(exc: Exception)` —— dataclass：先调用内层（产生副作用），再抛 `exc`（模拟"请求已达交易所、但 ack 丢失"的丢响应超时）。
  - 故障元素语义：`isinstance(fault, RaiseAfter)`→调内层后抛；`isinstance(fault, Exception)`→直接抛（请求未达内层）；`Partial`→见上；`None`/耗尽→透传。

- [ ] **Step 1: 写失败测试**

```python
# tests/exchanges/test_faulty.py
import ccxt
import pytest

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter, Partial, RaiseAfter

SYM = 'BTC/USDT:USDT'


def _fake():
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    return ex


def test_passthrough_when_no_schedule():
    f = FaultyAdapter(_fake())
    assert f.fetch_price(SYM) == 100.0
    assert f.name == 'fake'                      # 非可调用属性透传


def test_raises_scripted_exception_then_passes_through():
    f = FaultyAdapter(_fake(), {'fetch_price': [ccxt.RequestTimeout('t'), None]})
    with pytest.raises(ccxt.RequestTimeout):
        f.fetch_price(SYM)                       # 第1次：抛
    assert f.fetch_price(SYM) == 100.0           # 第2次：脚本耗尽 → 透传


def test_exception_fault_does_not_touch_inner():
    ex = _fake()
    f = FaultyAdapter(ex, {'create_limit_order': [ccxt.RequestTimeout('t')]})
    with pytest.raises(ccxt.RequestTimeout):
        f.create_limit_order(SYM, 'buy', 99.0, 0.01, client_oid='a:0:0')
    assert ex.fetch_open_orders(SYM) == []       # 请求未达内层 → 无挂单


def test_raise_after_calls_inner_then_raises():
    ex = _fake()
    f = FaultyAdapter(ex, {'create_limit_order': [RaiseAfter(ccxt.RequestTimeout('lost-ack'))]})
    with pytest.raises(ccxt.RequestTimeout):
        f.create_limit_order(SYM, 'buy', 99.0, 0.01, client_oid='a:0:0')
    assert len(ex.fetch_open_orders(SYM)) == 1   # 内层已建单（ack 丢失场景）


def test_partial_reduces_market_size_at_inner():
    ex = _fake()
    f = FaultyAdapter(ex, {'create_market_order': [Partial(0.5)]})
    o = f.create_market_order(SYM, 'buy', 1.0, client_oid='a:init:0')
    assert o.filled == pytest.approx(0.5)
    assert ex.fetch_positions(SYM).net_size == pytest.approx(0.5)  # 内层持仓只动一半
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_faulty.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'gridtrade.exchanges.faulty'`

- [ ] **Step 3: 写最小实现**

```python
# gridtrade/exchanges/faulty.py
"""FaultyAdapter：故障注入包装器（P6① 混沌测试）。透明包装任意 ExchangeAdapter，
按「方法名→故障列表」脚本消费故障，验证执行/对账/止损在异常下的端到端不变量。
鸭子类型（不继承 ABC），与 ResilientAdapter 同层（允许 import ccxt）。
"""
from dataclasses import dataclass


@dataclass
class Partial:
    """仅 create_market_order：内层下单量×ratio，模拟部分成交（HL 滑点/reduce 没吃满）。"""
    ratio: float


@dataclass
class RaiseAfter:
    """先调用内层（产生副作用）再抛 exc：模拟请求已达交易所但 ack 丢失的丢响应超时。"""
    exc: Exception


class FaultyAdapter:
    def __init__(self, inner, schedule=None):
        self._inner = inner
        self._schedule = {k: list(v) for k, v in (schedule or {}).items()}

    def _next_fault(self, name):
        q = self._schedule.get(name)
        if not q:
            return None
        return q.pop(0)

    def create_market_order(self, symbol, side, size, *, reduce_only=False, client_oid=None):
        fault = self._next_fault('create_market_order')
        if isinstance(fault, RaiseAfter):
            self._inner.create_market_order(symbol, side, size,
                                            reduce_only=reduce_only, client_oid=client_oid)
            raise fault.exc
        if isinstance(fault, Partial):
            size = size * fault.ratio
        elif isinstance(fault, Exception):
            raise fault
        return self._inner.create_market_order(symbol, side, size,
                                               reduce_only=reduce_only, client_oid=client_oid)

    def __getattr__(self, name):
        # 仅当属性未在本类正常解析时触发（_inner/_schedule/_next_fault/create_market_order 走正常解析）
        inner_attr = getattr(self._inner, name)
        if not callable(inner_attr):
            return inner_attr
        def wrapped(*args, **kwargs):
            fault = self._next_fault(name)
            if isinstance(fault, RaiseAfter):
                inner_attr(*args, **kwargs)
                raise fault.exc
            if isinstance(fault, Exception):
                raise fault
            return inner_attr(*args, **kwargs)
        return wrapped
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_faulty.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/faulty.py tests/exchanges/test_faulty.py
git commit -m "feat(exchanges): FaultyAdapter fault-injection wrapper (P6①)"
```

---

### Task 2: 场景 A —— 开仓中途超时 + 丢响应幂等（含 FakeExchange client_oid 去重）

**Files:**
- Create: `tests/execution/test_chaos_open.py`
- Modify（仅当红测试证实需要）: `gridtrade/exchanges/fake.py`（`create_limit_order` 加 client_oid 去重）

**Interfaces:**
- Consumes: Task 1 的 `FaultyAdapter`、`RaiseAfter`；`ResilientAdapter`、`RetryPolicy`、`GridExecutor`、`StateStore`。
- Produces: 复用的本地工厂 `build_stack(schedule=None, price=100.0) -> (fake, gx)`（定义在本测试文件内；后续 chaos 测试各自复制同款工厂，互不依赖）。

**说明（§7 测试驱动发现）：** `test_open_lost_ack_no_duplicate_order` 是不变量1（无重复单）的真正考验——它模拟"挂单请求已达交易所、ack 丢失、重试再发"。当前 FakeExchange `create_limit_order` 每次都新建 oid、不按 client_oid 去重，所以重试会产生**第二个挂单** → 红。修复是让 FakeExchange 像真实交易所那样**按 client_oid 去重已开挂单**（保真度提升，非故障逻辑）。在任务评审检查点向用户汇报后再合入此修改。

- [ ] **Step 1: 写测试（基线收敛 + 丢响应幂等）**

```python
# tests/execution/test_chaos_open.py
import random

import ccxt

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter, RaiseAfter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.store import StateStore

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def build_stack(schedule=None, price=100.0):
    fake = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    fake.set_price(SYM, price)
    resilient = ResilientAdapter(FaultyAdapter(fake, schedule or {}),
                                 policy=RetryPolicy(max_attempts=4),
                                 sleep=lambda _: None, rng=random.Random(0))
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    return fake, gx


def test_open_baseline_no_faults():
    fake, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    assert gx.grids.get(gid).status == 'ACTIVE'
    assert len(fake.fetch_open_orders(SYM)) == 8


def test_open_transient_timeout_still_reaches_active():
    # 前两次挂单请求未达交易所即超时 → ResilientAdapter 重试 → 仍开齐
    fake, gx = build_stack({'create_limit_order':
                            [ccxt.RequestTimeout('t'), ccxt.RequestTimeout('t')]})
    gid = gx.open('fake', SYM, GP)
    assert gx.grids.get(gid).status == 'ACTIVE'
    assert len(fake.fetch_open_orders(SYM)) == 8                  # 无缺单
    ids = [o.id for o in fake.fetch_open_orders(SYM)]
    assert len(ids) == len(set(ids))                             # 无重复单


def test_open_lost_ack_no_duplicate_order():
    # 第一笔挂单：内层已建单但 ack 丢失 → 重试再发 → 不得产生第二个挂单
    fake, gx = build_stack({'create_limit_order': [RaiseAfter(ccxt.RequestTimeout('lost-ack'))]})
    gid = gx.open('fake', SYM, GP)
    assert gx.grids.get(gid).status == 'ACTIVE'
    assert len(fake.fetch_open_orders(SYM)) == 8                  # 幂等：仍是 8，不是 9
    ids = [o.id for o in fake.fetch_open_orders(SYM)]
    assert len(ids) == len(set(ids))
```

- [ ] **Step 2: 跑测试，观察哪些失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_chaos_open.py -q`
Expected: `test_open_baseline_no_faults` PASS、`test_open_transient_timeout_still_reaches_active` PASS、`test_open_lost_ack_no_duplicate_order` **FAIL**（断言 9 != 8：FakeExchange 重试时新建了第二个挂单）。

- [ ] **Step 3: 检查点 —— 汇报发现，经确认后修 FakeExchange**

向用户汇报："丢响应重试会在 FakeExchange 产生重复挂单；真实交易所按 client_oid 去重，建议让 FakeExchange 同样去重以保真。" 确认后改 `create_limit_order`，在方法体最前加 client_oid 去重：

```python
    def create_limit_order(self, symbol, side, price, size, *,
                           post_only=False, reduce_only=False, client_oid=None) -> Order:
        if client_oid is not None:                       # 幂等：同 client_oid 已有未成交挂单 -> 返回原单
            for o in self._open.values():
                if o.client_oid == client_oid:
                    return o
        oid = str(next(self._ids))
        # ...（其余不变）
```

- [ ] **Step 4: 跑测试确认全绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_chaos_open.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 跑全套，确认未回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: PASS（全绿，新增数量增加）

- [ ] **Step 6: 提交**

```bash
git add tests/execution/test_chaos_open.py gridtrade/exchanges/fake.py
git commit -m "test(execution): chaos open under timeout + lost-ack idempotency; FakeExchange dedupes by client_oid (P6①)"
```

---

### Task 3: 场景 B —— 补单超时重试不漂移

**Files:**
- Create: `tests/execution/test_chaos_replenish.py`

**Interfaces:**
- Consumes: Task 1 的 `FaultyAdapter`；`ResilientAdapter`、`RetryPolicy`、`GridExecutor`、`StateStore`。
- Produces: 无（叶子测试；复制 Task 2 同款 `build_stack`）。

**说明：** 撮合后某格成交 → sync 补对侧单。在补单的 `create_limit_order` 上注入瞬时超时，断言重试后**恰好 1 个补单**、无双重摄入、记账与无故障基线逐字段一致。

- [ ] **Step 1: 写测试**

```python
# tests/execution/test_chaos_replenish.py
import random

import ccxt
import pytest

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.store import StateStore

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def build_stack(schedule=None, price=100.0):
    fake = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    fake.set_price(SYM, price)
    faulty = FaultyAdapter(fake, schedule or {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=4),
                                 sleep=lambda _: None, rng=random.Random(0))
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    return fake, faulty, gx


def _baseline_after_one_fill():
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 100.6)            # 穿越上方一格 -> 成交 -> sync 补对侧
    res = gx.sync(gid, SYM)
    snap = gx.live[gid].snapshot(fake.fetch_price(SYM))
    return res, snap, len(fake.fetch_open_orders(SYM))


def test_replenish_under_timeout_matches_baseline():
    base_res, base_snap, base_open = _baseline_after_one_fill()

    # 干净开仓后，仅在补单阶段注入超时（open 不受影响）
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    faulty._schedule['create_limit_order'] = [ccxt.RequestTimeout('t'),
                                              ccxt.RequestTimeout('t')]
    fake.set_price(SYM, 100.6)
    res = gx.sync(gid, SYM)
    snap = gx.live[gid].snapshot(fake.fetch_price(SYM))

    assert res['new_fills'] == base_res['new_fills']
    assert len(fake.fetch_open_orders(SYM)) == base_open            # 补单数与基线一致（无多补）
    for k in ('realized_pnl', 'net_position', 'fee_paid', 'avg_price'):
        assert snap[k] == pytest.approx(base_snap[k])              # 记账不漂移


def test_replenish_idempotent_on_resync():
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    open_after_first = len(fake.fetch_open_orders(SYM))
    res2 = gx.sync(gid, SYM)                                       # 二次 sync：无新成交
    assert res2['new_fills'] == 0
    assert len(fake.fetch_open_orders(SYM)) == open_after_first    # 不重复补单
```

- [ ] **Step 2: 跑测试确认通过（系统应已正确）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_chaos_replenish.py -q`
Expected: PASS（2 passed）。若 `test_replenish_under_timeout_matches_baseline` FAIL，转 systematic-debugging：是真实漂移 bug，停下汇报。

- [ ] **Step 3: 提交**

```bash
git add tests/execution/test_chaos_replenish.py
git commit -m "test(execution): chaos replenish under timeout retry stays consistent (P6①)"
```

---

### Task 4: 场景 C —— 对账遇孤儿/缺失 + 瞬时故障收敛

**Files:**
- Create: `tests/execution/test_chaos_reconcile.py`

**Interfaces:**
- Consumes: Task 1 的 `FaultyAdapter`；`Reconciler`、`ResilientAdapter`、`GridExecutor`。
- Produces: 无（叶子测试；复制同款 `build_stack`）。

**说明：** 构造缺失（交易所撤掉一个挂单、DB 仍记 open）+ 孤儿（交易所多一个非本网格意图单），并在补缺失的 `create_limit_order` 上注入一次超时；断言重试后收敛到期望单集，且再次对账幂等（0,0）。

- [ ] **Step 1: 写测试**

```python
# tests/execution/test_chaos_reconcile.py
import random

import ccxt

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.store import StateStore

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def build_stack(schedule=None, price=100.0):
    fake = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    fake.set_price(SYM, price)
    faulty = FaultyAdapter(fake, schedule or {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=4),
                                 sleep=lambda _: None, rng=random.Random(0))
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    return fake, faulty, gx


def test_reconcile_converges_despite_transient_fault():
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    rec = Reconciler(gx)

    # 缺失：交易所撤掉一个挂单（DB 仍 open）
    victim = fake.fetch_open_orders(SYM)[0]
    fake.cancel_order(SYM, victim.id)
    # 孤儿：交易所多挂一个非本网格意图单
    fake.create_limit_order(SYM, 'buy', 95.0, 0.5, client_oid='zzz:orphan:0')

    # 在补缺失单时注入一次瞬时超时
    faulty._schedule['create_limit_order'] = [ccxt.RequestTimeout('t')]
    out = rec.reconcile_open_orders(gid, SYM)
    assert out == {'canceled': 1, 'replaced': 1}                  # 重试后仍补回 + 撤孤儿
    assert all(o.client_oid != 'zzz:orphan:0' for o in fake.fetch_open_orders(SYM))
    assert len(fake.fetch_open_orders(SYM)) == 8                  # 收敛到期望单集

    out2 = rec.reconcile_open_orders(gid, SYM)                    # 再对账：幂等
    assert out2 == {'canceled': 0, 'replaced': 0}
```

- [ ] **Step 2: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_chaos_reconcile.py -q`
Expected: PASS（1 passed）。若 FAIL，转 systematic-debugging 并汇报（可能是 reconcile 在故障下未收敛的真实 bug）。

- [ ] **Step 3: 提交**

```bash
git add tests/execution/test_chaos_reconcile.py
git commit -m "test(execution): chaos reconcile converges under orphan/missing + transient fault (P6①)"
```

---

### Task 5: 场景 D —— 平仓部分成交残留（特征化 + 决策点）

**Files:**
- Create: `tests/execution/test_chaos_close.py`

**Interfaces:**
- Consumes: Task 1 的 `FaultyAdapter`、`Partial`；`GridExecutor`、`ResilientAdapter`。
- Produces: 无（叶子测试；复制同款 `build_stack`）。

**说明（§7 决策点）：** `close()` 是终态、reduce 市价单不重试。注入 `Partial(0.5)` 让平仓只吃一半 → 交易所留残仓。本测试**特征化当前行为**（断言残仓存在），暴露 gap；在检查点向用户汇报，由用户决定"补残仓校验/补平"还是"记为已知限制"。不预先假定要改生产代码。

- [ ] **Step 1: 写特征化测试**

```python
# tests/execution/test_chaos_close.py
import random

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter, Partial
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.store import StateStore

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def build_stack(schedule=None, price=100.0):
    fake = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    fake.set_price(SYM, price)
    faulty = FaultyAdapter(fake, schedule or {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=4),
                                 sleep=lambda _: None, rng=random.Random(0))
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    return fake, faulty, gx


def test_close_clean_flattens_position_baseline():
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)               # 中性底仓 -> 持有多头净仓
    assert fake.fetch_positions(SYM).net_size > 0
    gx.close(gid, SYM, '测试平仓')
    assert gx.grids.get(gid).status == 'CLOSED'
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-9   # 无故障：平干净


def test_close_partial_fill_leaves_residual_position():
    # 特征化当前行为：reduce 市价单只吃一半 -> 残仓 -> close() 仍转 CLOSED（不补平）
    fake, faulty, gx = build_stack()
    gid = gx.open('fake', SYM, GP)
    net_before = fake.fetch_positions(SYM).net_size
    assert net_before > 0
    faulty._schedule['create_market_order'] = [Partial(0.5)]   # 平仓 reduce 只成交一半
    gx.close(gid, SYM, '测试平仓')
    residual = fake.fetch_positions(SYM).net_size
    assert residual > 1e-9                                      # 残仓存在（gap）
    assert gx.grids.get(gid).status == 'CLOSED'                 # 当前实现仍判定已平
```

- [ ] **Step 2: 跑测试确认通过（特征化当前行为）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_chaos_close.py -q`
Expected: PASS（2 passed）—— 测试**记录**了 gap（部分成交留残仓），非红测试。

- [ ] **Step 3: 检查点 —— 向用户汇报决策**

汇报："场景 D 证实：平仓部分成交会留残仓且网格仍转 CLOSED。选项：(a) 在 close 后校验持仓、对残仓补一笔 reduce；(b) 记为已知限制（monitor 下轮对账兜底）。" 按用户选择：若选 (a)，在本任务追加红→绿（先加断言残仓被补平的测试，再改 `close`）；若选 (b)，保留特征化测试 + 在 STATUS.md §9 记一条已知限制。

- [ ] **Step 4: 提交**

```bash
git add tests/execution/test_chaos_close.py
git commit -m "test(execution): characterize close partial-fill residual position (P6①)"
```

---

### Task 6: 场景 E —— monitor cycle per-grid 故障隔离（特征化 + 决策点）

**Files:**
- Create: `tests/runtime/test_chaos_cycle.py`
- Modify（仅当用户选择修复）: `gridtrade/runtime/cycles.py:24-33`（`run_monitor_cycle` 加 per-grid try/except）

**Interfaces:**
- Consumes: Task 1 的 `FaultyAdapter`；`run_monitor_cycle`、`Reconciler`、`GridManager`、`GateChain`、`GridExecutor`。
- Produces: 无（叶子测试）。若修复：`run_monitor_cycle` 返回结构新增每网格降级标记（见 Step 3）。

**说明（§7 决策点）：** 现 `run_monitor_cycle` 逐网格 reconcile 无 try/except；一个网格的故障耗尽重试后抛异常，会掀翻**整轮**cycle（其他健康网格也得不到对账/补单/止损）。本测试用两个网格、对其中一个注入持续故障，特征化"当前整轮抛异常"；检查点决定是否加 per-grid 隔离。

- [ ] **Step 1: 写特征化测试**

```python
# tests/runtime/test_chaos_cycle.py
import random

import ccxt
import pytest

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter
from gridtrade.exchanges.resilience import RetryPolicy
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.manager import GridManager
from gridtrade.execution.gates import GateChain
from gridtrade.runtime.cycles import run_monitor_cycle
from gridtrade.state.store import StateStore

SYM_A = 'BTC/USDT:USDT'
SYM_B = 'ETH/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': -0.5, 'take_profit': 1.0, 'trailing': 0.3}


def build():
    insts = [Instrument(SYM_A, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(SYM_B, 0.1, 0.001, 0.001, 'live', 0)]
    fake = FakeExchange(instruments=insts, price=100.0)
    fake.set_price(SYM_A, 100.0); fake.set_price(SYM_B, 100.0)
    faulty = FaultyAdapter(fake, {})
    resilient = ResilientAdapter(faulty, policy=RetryPolicy(max_attempts=2),
                                 sleep=lambda _: None, rng=random.Random(0))
    store = StateStore.in_memory(); store.create_all()
    gx = GridExecutor(resilient, store, cap=1000.0, leverage=5.0)
    mgr = GridManager(gx, GateChain([]), stop_cfg=STOP_CFG)
    return fake, faulty, gx, mgr


def test_one_bad_grid_currently_aborts_whole_cycle():
    fake, faulty, gx, mgr = build()
    gx.open('fake', SYM_A, GP)
    gx.open('fake', SYM_B, GP)
    rec = Reconciler(gx)
    # 对 A 币种的对账注入持续故障：fetch_open_orders 始终维护中 -> 重试耗尽抛
    faulty._schedule['fetch_open_orders'] = [ccxt.OnMaintenance('m')] * 50
    with pytest.raises(ccxt.OnMaintenance):           # 特征化：整轮被掀翻
        run_monitor_cycle(rec, mgr)
```

- [ ] **Step 2: 跑测试确认通过（特征化当前行为）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_chaos_cycle.py -q`
Expected: PASS（1 passed）—— 记录"当前整轮抛异常"。

- [ ] **Step 3: 检查点 —— 向用户汇报决策；若选修复则 per-grid 隔离**

汇报："场景 E 证实：单网格对账故障会掀翻整轮 monitor cycle，健康网格被殃及。建议加 per-grid try/except 降级（坏网格记错误、跳过，不阻塞其他）。" 若用户同意，改 `run_monitor_cycle`，并把上面的 `pytest.raises` 测试改为断言"健康网格仍被处理、坏网格被标记降级"：

```python
# gridtrade/runtime/cycles.py  —— run_monitor_cycle 替换逐网格循环体
def run_monitor_cycle(reconciler, manager) -> dict:
    """monitor 机循环体：逐网格隔离——单网格故障降级记录，不阻塞其他网格。"""
    ex = manager.executor
    reconciled = {}
    degraded = {}
    for grid in _active_grids(ex.grids):
        try:
            if not ex.is_loaded(grid.id):
                reconciler.restore(grid.id)
            reconciled[grid.id] = reconciler.reconcile_open_orders(grid.id, grid.symbol)
        except Exception as exc:                      # 降级：坏网格不掀翻整轮（绝不吞 BaseException）
            degraded[grid.id] = repr(exc)
    monitored = manager.monitor_all()
    return {'reconciled': reconciled, 'degraded': degraded, 'monitored': monitored}
```

并把测试改为：

```python
def test_one_bad_grid_does_not_block_healthy_grid():
    fake, faulty, gx, mgr = build()
    gid_a = gx.open('fake', SYM_A, GP)
    gid_b = gx.open('fake', SYM_B, GP)
    rec = Reconciler(gx)
    faulty._schedule['fetch_open_orders'] = [ccxt.OnMaintenance('m')] * 50
    out = run_monitor_cycle(rec, mgr)
    assert gid_a in out['degraded']                   # 坏网格被降级记录
    assert gid_b in out['reconciled']                 # 健康网格仍完成对账
```

注：`manager.monitor_all()` 内 `monitor_grid` 对 A 仍会调 sync→fetch_my_trades；若该路径也需隔离，作为后续 follow-up 记入 STATUS.md，本任务聚焦 reconcile 隔离。

- [ ] **Step 4: 跑测试 + 全套**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_chaos_cycle.py -q && TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: PASS（全绿）

- [ ] **Step 5: 提交**

```bash
git add tests/runtime/test_chaos_cycle.py gridtrade/runtime/cycles.py
git commit -m "feat(runtime): per-grid fault isolation in monitor cycle + chaos test (P6①)"
```

---

### Task 7: 收尾 —— 更新 STATUS.md + 全套回归

**Files:**
- Modify: `docs/STATUS.md`（§3 阶段历史加 P6①行；§7/§9 据 Task 5/6 决策更新；测试数更新）

**Interfaces:**
- Consumes: 前 6 个任务结果。
- Produces: 无。

- [ ] **Step 1: 跑全套确认绿 + 记录测试数**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q 2>&1 | tail -3`
Expected: `N passed`（N > 258）

- [ ] **Step 2: 更新 STATUS.md**

在 §3 阶段历史表加一行 `| P6① | 故障注入 FaultyAdapter + 混沌测试（开仓/补单/对账/平仓/cycle 隔离）|`；把顶部"258 tests passed"更新为新数字；据 Task 5（场景 D）与 Task 6（场景 E）的最终决策，更新 §7（testnet 验证状态）或 §9（仍延后/已知限制）相应条目。

- [ ] **Step 3: 提交**

```bash
git add docs/STATUS.md
git commit -m "docs: STATUS update for P6① chaos/fault-injection"
```

---

## 自检（Self-Review）

- **Spec 覆盖**：§3 FaultyAdapter→Task1；§5 场景 A→Task2、B→Task3、C→Task4、D→Task5、E→Task6；§6 不变量分散于各任务断言（无重复单 Task2、无双补 Task3/4、记账不漂移 Task3、收敛 Task4、不崩 Task6）；§7 两条加固→Task2(client_oid 去重发现)+Task6(per-grid 隔离)+Task5(残仓决策)；§9 交付物→各任务文件。覆盖完整。
- **占位符**：无 TBD/TODO；每个代码步给出完整代码。
- **类型/命名一致**：`FaultyAdapter` / `Partial(ratio)` / `RaiseAfter(exc)` / `build_stack` / `_next_fault` 全程一致；`run_monitor_cycle` 返回新增 `degraded` 键在 Task6 自洽。
- **决策点显式标注**：Task2 Step3、Task5 Step3、Task6 Step3 均为"先证实再经用户确认改生产代码"的检查点，符合 spec §7 打法。
