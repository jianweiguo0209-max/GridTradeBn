# OrderFilled 事件 + fetch_funding_range 离线缓存测试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ① 补事件总线第 3 事件 `OrderFilled`（成交时由 GridManager 发布，带手续费）；② 给 `DataSource.fetch_funding_range` 补离线缓存测试（覆盖目前裸奔的 `time_col='ts'` 分支）。

**Architecture:** OrderFilled 走「sync 收集新成交 dict → monitor_grid 透传 → manager 发布」，executor 保持 bus-free（与 GridOpened/GridClosed 同模式）。funding 测试镜像现有 ohlcv 测试，换 funding 路径。

**Tech Stack:** Python 3.9 / pytest / pandas / SQLAlchemy（内存 SQLite）。

## Global Constraints

- 跑测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest`（仓库 `.venv`）。
- 现有 274 测试保持全绿；改动均为加法。
- `gridtrade/execution/` 不 import ccxt；**executor 不 import events**（manager 才发布事件）。
- OrderFilled 字段顺序固定：`grid_id, symbol, line_index, side, price, size, fee`。
- 成交去重靠 `grid_fills.trade_id`（幂等）——只对新摄入成交发事件。

---

### Task 1: OrderFilled 事件（sync 收集 → monitor_grid 透传 → manager 发布）

**Files:**
- Modify: `gridtrade/execution/events.py`（加 `OrderFilled` dataclass）
- Modify: `gridtrade/execution/grid_executor.py`（`sync` 收集并返回 `'fills'`）
- Modify: `gridtrade/execution/monitor.py`（`monitor_grid` 透传 `'fills'`）
- Modify: `gridtrade/execution/manager.py`（`monitor_all` 发布 OrderFilled + import）
- Test: `tests/execution/test_events.py`（事件 dataclass）、`tests/execution/test_manager.py`（发布行为）

**Interfaces:**
- Consumes: `GridManager`/`GridExecutor`/`monitor_grid`/`EventBus`（均已存在）；`Trade.fee`（成交手续费，已在 base.Trade）。
- Produces:
  - `OrderFilled(grid_id: str, symbol: str, line_index: int, side: str, price: float, size: float, fee: float)`
  - `GridExecutor.sync` 返回新增 `'fills'` 键：`list[dict]`，每条 `{'line_index': int, 'side': str, 'price': float, 'size': float, 'fee': float, 'ts': int}`。

- [ ] **Step 1: 写失败测试（事件 dataclass + 发布行为）**

在 `tests/execution/test_events.py` 末尾追加：

```python
def test_orderfilled_event_fields():
    from gridtrade.execution.events import OrderFilled
    e = OrderFilled(grid_id='g1', symbol='BTC/USDT:USDT', line_index=3,
                    side='sell', price=100.6, size=0.5, fee=0.02)
    assert (e.grid_id, e.symbol, e.line_index, e.side, e.price, e.size, e.fee) == \
        ('g1', 'BTC/USDT:USDT', 3, 'sell', 100.6, 0.5, 0.02)
```

在 `tests/execution/test_manager.py` 末尾追加（复用文件内既有 `_setup`/`_proposal`/`_manager`/`SYM`/`GP`）：

```python
def test_monitor_all_publishes_orderfilled_per_new_fill():
    from gridtrade.execution.events import EventBus, OrderFilled
    ex, store, gx = _setup(100.0)
    bus = EventBus(); filled = []
    bus.subscribe(lambda e: filled.append(e) if isinstance(e, OrderFilled) else None)
    mgr = _manager(gx, store, bus)
    mgr.open_proposals([_proposal()])
    ex.set_price(SYM, 100.6)              # 穿越上方一格 -> 成交
    out = mgr.monitor_all()
    # 事件数 == monitor_all 实报的本轮新成交数（不硬编码几何），且确有成交
    fills_reported = out[0]['fills']
    assert len(filled) == len(fills_reported) >= 1
    e = filled[0]
    assert e.symbol == SYM and e.side == 'sell' and e.size > 0 and e.fee > 0
    # 二次 monitor 无新成交 -> 不再发（幂等）
    filled.clear()
    mgr.monitor_all()
    assert filled == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_events.py::test_orderfilled_event_fields tests/execution/test_manager.py::test_monitor_all_publishes_orderfilled_per_new_fill -q`
Expected: FAIL —— `ImportError: cannot import name 'OrderFilled'`（事件未定义）。

- [ ] **Step 3a: 加 OrderFilled 事件**

在 `gridtrade/execution/events.py` 的 `GridClosed` 之后插入：

```python
@dataclass
class OrderFilled:
    grid_id: str
    symbol: str
    line_index: int
    side: str
    price: float
    size: float
    fee: float
```

- [ ] **Step 3b: sync 收集新成交并返回 'fills'**

在 `gridtrade/execution/grid_executor.py` 的 `sync` 方法中：

把 `new_count = 0` 这一行改为：

```python
        new_count = 0
        new_fills_payload = []
```

在成交循环里，紧跟 `new_count += 1` 之后加一行收集（`t` 为该笔交易所成交 `Trade`，带真实 `fee`）：

```python
            new_count += 1
            new_fills_payload.append({'line_index': line_index, 'side': t.side,
                                      'price': float(t.price), 'size': float(t.size),
                                      'fee': float(t.fee), 'ts': int(t.ts)})
```

把方法末尾的 `return {'new_fills': new_count, 'snapshot': snap}` 改为：

```python
        return {'new_fills': new_count, 'fills': new_fills_payload, 'snapshot': snap}
```

- [ ] **Step 3c: monitor_grid 透传 'fills'**

在 `gridtrade/execution/monitor.py` 的 `monitor_grid` 中，把两个 return 改为带 `fills`：

```python
    if reason:
        executor.close(grid_id, symbol, reason)
        return {'closed': True, 'reason': reason, 'pnl_ratio': snap['pnl_ratio'],
                'fills': res.get('fills', [])}
    return {'closed': False, 'reason': None, 'pnl_ratio': snap['pnl_ratio'],
            'fills': res.get('fills', [])}
```

- [ ] **Step 3d: manager 发布 OrderFilled**

在 `gridtrade/execution/manager.py`：

import 行 `from gridtrade.execution.events import GridOpened, GridClosed` 改为：

```python
from gridtrade.execution.events import GridOpened, GridClosed, OrderFilled
```

在 `monitor_all` 的 except 块之后、`if res['closed']:` 之前插入发布循环：

```python
            for f in res.get('fills', []):
                self._publish(OrderFilled(
                    grid_id=grid.id, symbol=grid.symbol, line_index=f['line_index'],
                    side=f['side'], price=f['price'], size=f['size'], fee=f['fee']))
            if res['closed']:
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_events.py tests/execution/test_manager.py -q`
Expected: PASS（含既有用例不受影响）。

- [ ] **Step 5: 跑全套确认无回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿（≥276 passed）。

- [ ] **Step 6: 提交**

```bash
git add gridtrade/execution/events.py gridtrade/execution/grid_executor.py \
        gridtrade/execution/monitor.py gridtrade/execution/manager.py \
        tests/execution/test_events.py tests/execution/test_manager.py
git commit -m "feat(execution): OrderFilled event published per new fill (with fee)"
```

---

### Task 2: fetch_funding_range 离线缓存测试

**Files:**
- Test: `tests/backtest/test_datasource.py`（新增 funding 用例；复用文件内既有 `_ds`、`SYM`、`DAY`）

**Interfaces:**
- Consumes: `DataSource.fetch_funding_range(symbol, start_ms, end_ms)`、`FakeExchange.seed_funding(symbol, df)`、
  `ParquetCache`、`gridtrade.exchanges.base.FUNDING_COLS`（`['ts','symbol','fundingRate','realizedRate']`）。
- Produces: 无（叶子测试）。

- [ ] **Step 1: 写测试（warm→离线 + 任意覆盖窗口完全复用 + 空哨兵）**

在 `tests/backtest/test_datasource.py` 末尾追加：

```python
def _funding(start_ms, n, step_ms=8 * 3600_000):
    ts = [start_ms + i * step_ms for i in range(n)]
    return pd.DataFrame({
        'ts': ts, 'symbol': SYM,
        'fundingRate': [0.0001 * (i + 1) for i in range(n)],
        'realizedRate': [0.0001 * (i + 1) for i in range(n)],
    })


class _OfflineFunding(FakeExchange):
    def fetch_funding_history(self, *a, **k):
        raise AssertionError('should not hit network after warm')


def test_funding_range_warms_then_serves_offline(tmp_path):
    from gridtrade.exchanges.base import FUNDING_COLS
    start = 1_704_067_200_000                      # 2024-01-01 00:00 UTC
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_funding(SYM, _funding(start, 6))       # 2 天、8h 间隔 -> 6 行
    ds = _ds(tmp_path, ex)
    end = start + 2 * DAY - 1
    df1 = ds.fetch_funding_range(SYM, start, end)
    assert list(df1.columns) == FUNDING_COLS and len(df1) == 6

    off = _OfflineFunding(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds2 = _ds(tmp_path, off)                        # 同一 cache、触网即报错
    df2 = ds2.fetch_funding_range(SYM, start, end)
    assert len(df2) == 6 and list(df2['fundingRate']) == list(df1['fundingRate'])


def test_funding_range_any_covered_window_fully_offline(tmp_path):
    # 预热后，区间内任意子窗口都应完全由缓存服务、不触网
    start = 1_704_067_200_000
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_funding(SYM, _funding(start, 6))
    ds = _ds(tmp_path, ex)
    ds.fetch_funding_range(SYM, start, start + 2 * DAY - 1)   # warm 2 days
    off = _OfflineFunding(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds2 = _ds(tmp_path, off)
    sub = ds2.fetch_funding_range(SYM, start, start + DAY - 1)  # 第 1 天子窗口
    assert len(sub) == 3 and all(sub['ts'] < start + DAY)       # 仅第 1 天 3 行，纯离线


def test_funding_range_empty_day_sentinel_offline(tmp_path):
    # 某天无资金费 -> 落空哨兵 -> 离线仍正常（不报错、不并入空数据）
    start = 1_704_067_200_000
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ex.seed_funding(SYM, _funding(start, 3))        # 仅第 1 天有 3 行；第 2 天空
    ds = _ds(tmp_path, ex)
    ds.fetch_funding_range(SYM, start, start + 2 * DAY - 1)   # warm 2 days（第 2 天空哨兵）
    off = _OfflineFunding(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)])
    ds2 = _ds(tmp_path, off)
    df = ds2.fetch_funding_range(SYM, start, start + 2 * DAY - 1)
    assert len(df) == 3                              # 第 2 天空哨兵不并入垃圾行
```

- [ ] **Step 2: 跑测试**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_datasource.py -q`
Expected: 3 个新用例 PASS。若某条变红（如子窗口触网、空哨兵并入垃圾、行数错），说明 `time_col='ts'` 分桶分支藏 bug —— **停下汇报**（先红暴露，再决定修），勿改测试迁就。

- [ ] **Step 3: 提交**

```bash
git add tests/backtest/test_datasource.py
git commit -m "test(backtest): fetch_funding_range warm/offline/subset/sentinel coverage"
```

---

## 自检（Self-Review）

- **Spec 覆盖**：第 1 项「manager 发布 / sync 收集 fills(含 fee) / monitor_grid 透传 / 新事件」→ Task 1 Step 3a–3d；第 1 项测试（收 OrderFilled、字段、幂等）→ Task 1 Step 1。第 2 项「warm→离线 / 任意覆盖窗口复用 / 空哨兵」→ Task 2 三个用例。覆盖完整。
- **占位符**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型/命名一致**：`OrderFilled(grid_id,symbol,line_index,side,price,size,fee)` 在 events / sync payload / manager 发布三处字段一致；sync `'fills'` dict 键 = manager 读取键一致；`_funding`/`_OfflineFunding`/`_ds` 自洽。
- **executor 不 import events**：Task 1 中 executor 只返回普通 dict，事件类型仅在 manager/测试出现——符合约束。
