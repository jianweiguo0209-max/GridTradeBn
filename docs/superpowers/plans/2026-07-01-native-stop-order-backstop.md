# 交易所原生止损单（灾难保险丝）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给网格补一道交易所原生 reduce-only 触发单作为灾难保险丝（破网价触发、最坏仓+对账重挂、触发后撑网全拆），堵住软止损在跳空/宕机/熔断/5s 盲区下失效的结构性风险。

**Architecture:** `ExchangeAdapter` 加 `create_stop_order` 端口（CcxtAdapter 实现、OKX/HL 继承、Fake 撮合、Resilient 包装、Faulty 自动透传）。`GridExecutor.open()` 建网后挂两张 reduce-only 保险丝，exchange order id 持久化到 `grids` 行（跨重启可判定已触发）。`Reconciler.reconcile_fuses` 每轮判定在挂/被丢/已触发，分别无动作/重挂/撑网全拆。软止损完全不动、仍是主刹车。

**Tech Stack:** Python 3.9 / SQLAlchemy 2.0 Core / ccxt 4.5.61 / pytest（双后端 SQLite+Postgres，`store` fixture）。

## Global Constraints

- 设计文档（单一事实源）：`docs/superpowers/specs/2026-07-01-native-stop-order-backstop-design.md`。
- `core/` 不依赖任何交易所库；本特性零触碰 core 与 LiveEquity 记账数学。
- 保险丝成交**不进** `grid_fills`（与 init/close 市价单一致）：保险丝**不写** `grid_orders` 表、其 exchange order id 仅存 `grids.fuse_low_oid`/`fuse_high_oid`。
- 跨所匹配靠 **exchange order id**（HL 成交只带 oid、不带 cloid）。
- 配置默认：`STOP_SLIPPAGE=0.15`、`STOP_ORDERS_ENABLED=true`（env 覆盖）。
- `GridExecutor` 构造参数 `stop_orders_enabled` 默认 **False**（不破坏既有不传该参的测试）；`factory` 传 `config.stop_orders_enabled`(True)。
- 跑测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest`。
- 提交信息结尾加：`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `gridtrade/exchanges/base.py` | 端口 | 加抽象方法 `create_stop_order` |
| `gridtrade/exchanges/ccxt_adapter.py` | 通用 ccxt 实现（OKX/HL 继承） | 加 `create_stop_order` |
| `gridtrade/exchanges/fake.py` | 测试撮合 | `_stops` 待触发簿 + `_check_stops` + reduce-only 封顶 + cancel_all 清 stops |
| `gridtrade/exchanges/resilient_adapter.py` | 重试包装 | 加 `create_stop_order` |
| `gridtrade/state/models.py` | 表/数据类 | `grids` 加 `fuse_low_oid`/`fuse_high_oid` 列 + Grid 数据类两字段 |
| `gridtrade/state/grids.py` | 仓储 | `_FIELDS` 加两字段 + `set_fuse_oids` |
| `gridtrade/runtime/dbadmin.py` | 迁移 | 幂等加两列 |
| `gridtrade/config.py` | 配置 | `stop_orders_enabled` / `stop_slippage` |
| `gridtrade/execution/grid_executor.py` | 执行 | `open()` 挂保险丝、`finalize_close()` 撤保险丝、`restore` 缓存、构造参数 |
| `gridtrade/execution/reconciler.py` | 对账 | `reconcile_fuses` + `_fuse_filled` |
| `gridtrade/runtime/cycles.py` | 循环 | monitor 轮调 `reconcile_fuses` |
| `gridtrade/runtime/factory.py` | 组装 | 把开关/滑点接进 executor |

FaultyAdapter 经 `__getattr__` 自动透传新方法，**无需改动**（验证即可）。

---

## Task 1: 适配器端口 `create_stop_order` + Fake 撮合

**Files:**
- Modify: `gridtrade/exchanges/base.py`（`ExchangeAdapter` 加抽象方法）
- Modify: `gridtrade/exchanges/ccxt_adapter.py:156`（`create_market_order` 之后加）
- Modify: `gridtrade/exchanges/fake.py`（`__init__` 加 `_stops`、加 `create_stop_order`/`_check_stops`、改 `set_price`/`cancel_all`）
- Modify: `gridtrade/exchanges/resilient_adapter.py:64`（`create_market_order` 之后加）
- Test: `tests/exchanges/test_fake_stop_order.py`（新建）

**Interfaces:**
- Produces:
  `ExchangeAdapter.create_stop_order(self, symbol, side, size, trigger_price, *, reduce_only=True, slippage=0.15, client_oid=None) -> Order`
  - 触发市价单：触发价被价格穿越后按市价 reduce-only 成交；`reduce_only=True` 时成交量封顶到当前反向持仓，无反向持仓则空操作（不成交、留在簿上）。
  - Fake：穿越判据 `side=='sell' and price<=trigger` 或 `side=='buy' and price>=trigger`；成交后从待触发簿移除、`fetch_my_trades` 可见（带 `order_id`）。

- [ ] **Step 1: 写失败测试**

`tests/exchanges/test_fake_stop_order.py`：
```python
from gridtrade.exchanges.fake import FakeExchange


def _long_5(ex, sym):
    """建立 +5 的多头持仓，便于测 reduce-only。"""
    ex.set_price(sym, 100.0)
    ex.create_market_order(sym, 'buy', 5.0)


def test_stop_not_filled_until_crossed():
    ex = FakeExchange()
    _long_5(ex, 'X')
    o = ex.create_stop_order('X', 'sell', 5.0, 90.0)   # 跌破 90 才触发
    assert o.status == 'open'
    ex.set_price('X', 95.0)                              # 未穿
    assert ex.fetch_open_orders('X')  # 网格无关单不在这里，但 stop 不应成交
    assert not any(t.order_id == o.id for t in ex.fetch_my_trades('X', since_ms=0))


def test_stop_fills_when_crossed_and_reduces_position():
    ex = FakeExchange()
    _long_5(ex, 'X')
    o = ex.create_stop_order('X', 'sell', 5.0, 90.0)
    ex.set_price('X', 89.0)                              # 穿破触发价
    fills = [t for t in ex.fetch_my_trades('X', since_ms=0) if t.order_id == o.id]
    assert len(fills) == 1
    assert ex.fetch_positions('X').net_size == 0.0       # 多头被平


def test_reduce_only_caps_to_position():
    ex = FakeExchange()
    _long_5(ex, 'X')                                     # 仅 +5
    o = ex.create_stop_order('X', 'sell', 999.0, 90.0)   # size 远超持仓
    ex.set_price('X', 89.0)
    fill = [t for t in ex.fetch_my_trades('X', since_ms=0) if t.order_id == o.id][0]
    assert fill.size == 5.0                               # 封顶到持仓，不反手
    assert ex.fetch_positions('X').net_size == 0.0


def test_reduce_only_noop_without_opposite_position():
    ex = FakeExchange()
    ex.set_price('X', 100.0)                              # 无持仓
    o = ex.create_stop_order('X', 'sell', 5.0, 90.0)
    ex.set_price('X', 89.0)
    assert not any(t.order_id == o.id for t in ex.fetch_my_trades('X', since_ms=0))
    assert o in ex._stops.get('X', [])                   # 空操作，留在簿上


def test_cancel_all_clears_stops():
    ex = FakeExchange()
    _long_5(ex, 'X')
    ex.create_stop_order('X', 'sell', 5.0, 90.0)
    ex.cancel_all('X')
    assert not ex._stops.get('X')
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_fake_stop_order.py -v`
Expected: FAIL（`FakeExchange` 无 `create_stop_order` / 无 `_stops`）。

- [ ] **Step 3: base 加抽象方法**

`gridtrade/exchanges/base.py`，在 `create_market_order` 抽象方法之后插入：
```python
    @abstractmethod
    def create_stop_order(self, symbol: str, side: str, size: float,
                          trigger_price: float, *,
                          reduce_only: bool = True, slippage: float = 0.15,
                          client_oid: Optional[str] = None) -> Order:
        """交易所原生触发市价单（灾难保险丝）。trigger_price=触发价；触发后市价成交，
        成交底线 = trigger_price×(1∓slippage)；reduce_only 默认 True。"""
        ...
```

- [ ] **Step 4: CcxtAdapter 实现**

`gridtrade/exchanges/ccxt_adapter.py`，在 `create_market_order` 之后插入：
```python
    def create_stop_order(self, symbol, side, size, trigger_price, *,
                          reduce_only=True, slippage=0.15, client_oid=None) -> Order:
        # 触发市价单：stopLossPrice -> HL tpsl='sl'；参考价传触发价本身，
        # 故成交底线 = trigger_price×(1∓slippage)，slippage 控制为保成交愿追多远。
        p = self._params(reduce_only, client_oid)
        p['stopLossPrice'] = trigger_price
        p['slippage'] = slippage
        r = self.client.create_order(self.to_native(symbol), 'market', side, size,
                                     trigger_price, p)
        return self._to_order(r)
```

- [ ] **Step 5: FakeExchange 实现撮合**

`gridtrade/exchanges/fake.py`：
1. `__init__` 里（其他 `self._...` 初始化旁）加：`self._stops = {}`。
2. `set_price` 改为触发停损也检查：
```python
    def set_price(self, symbol: str, price: float) -> None:
        self._price[symbol] = price
        self._match(symbol, price)
        self._check_stops(symbol, price)
```
3. 加方法（放在 `_match`/`_fill` 附近）：
```python
    def create_stop_order(self, symbol, side, size, trigger_price, *,
                          reduce_only=True, slippage=0.15, client_oid=None) -> Order:
        oid = str(next(self._ids))
        o = Order(id=oid, client_oid=client_oid or oid, symbol=symbol, side=side,
                  price=trigger_price, size=size, filled=0.0, status='open',
                  reduce_only=reduce_only)
        self._stops.setdefault(symbol, []).append(o)
        return o

    def _check_stops(self, symbol: str, price: float) -> None:
        for o in list(self._stops.get(symbol, [])):
            crossed = (o.side == 'sell' and price <= o.price) or \
                      (o.side == 'buy' and price >= o.price)
            if not crossed:
                continue
            pos = self._pos.get(symbol, Position(symbol, 0.0, 0.0))
            if o.reduce_only:
                # 只在有反向持仓时成交，size 封顶到持仓
                if o.side == 'sell' and pos.net_size > 0:
                    fill_size = min(o.size, pos.net_size)
                elif o.side == 'buy' and pos.net_size < 0:
                    fill_size = min(o.size, -pos.net_size)
                else:
                    continue   # 无可减仓位 -> 空操作，留在簿上
            else:
                fill_size = o.size
            filled = Order(id=o.id, client_oid=o.client_oid, symbol=symbol,
                           side=o.side, price=o.price, size=fill_size, filled=fill_size,
                           status='closed', reduce_only=o.reduce_only)
            self._fill(filled, price)
            self._stops[symbol].remove(o)
```
4. `cancel_all` 末尾加清停损簿：
```python
    def cancel_all(self, symbol) -> None:
        for oid in [k for k, v in self._open.items() if v.symbol == symbol]:
            del self._open[oid]
        self._stops.pop(symbol, None)
```

- [ ] **Step 6: ResilientAdapter 包装**

`gridtrade/exchanges/resilient_adapter.py`，在 `create_market_order` 之后插入：
```python
    def create_stop_order(self, symbol: str, side: str, size: float,
                          trigger_price: float, *, reduce_only: bool = True,
                          slippage: float = 0.15,
                          client_oid: Optional[str] = None) -> Order:
        return self._call('create_stop_order', symbol, side, size, trigger_price,
                          reduce_only=reduce_only, slippage=slippage,
                          client_oid=client_oid)
```

- [ ] **Step 7: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_fake_stop_order.py -v`
Expected: PASS（5 个）。

- [ ] **Step 8: 跑全套确认无回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿（既有数量 + 5）。注意：base 新增 abstractmethod 后，所有具体适配器须已实现——CcxtAdapter/Fake/Resilient 已加，OKX/HL 继承 CcxtAdapter。若有测试用桩适配器直接继承 `ExchangeAdapter` 而未实现该方法会报错，按需在该桩补一个 `raise NotImplementedError` 实现。

- [ ] **Step 9: Commit**

```bash
git add gridtrade/exchanges/base.py gridtrade/exchanges/ccxt_adapter.py \
        gridtrade/exchanges/fake.py gridtrade/exchanges/resilient_adapter.py \
        tests/exchanges/test_fake_stop_order.py
git commit -m "$(printf 'feat(exchanges): create_stop_order 端口 + Fake 触发撮合\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 2: 状态层 — grids 加 fuse oid 两列 + 迁移

**Files:**
- Modify: `gridtrade/state/models.py`（`grids` Table 加两列 + Grid 数据类两字段）
- Modify: `gridtrade/state/grids.py`（`_FIELDS` 加两字段 + `set_fuse_oids`）
- Modify: `gridtrade/runtime/dbadmin.py`（迁移加两列）
- Test: `tests/state/test_grid_fuse_oids.py`（新建）

**Interfaces:**
- Consumes: `GridRepository`（Task 0 既有）。
- Produces:
  - `Grid` 数据类新增 `fuse_low_oid: Optional[str] = None`、`fuse_high_oid: Optional[str] = None`。
  - `GridRepository.set_fuse_oids(self, grid_id, *, low_oid=_UNSET, high_oid=_UNSET) -> None`（只更新显式传入的列，不动 version/不改状态机）。
  - `dbadmin.add_grids_fuse_oids(store) -> 'added'|'skipped'`，并入 `migrate`。

- [ ] **Step 1: 写失败测试**

`tests/state/test_grid_fuse_oids.py`：
```python
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import Grid


def _new_grid(repo):
    return repo.create(Grid(id='', exchange='hl', symbol='BTC/USDC:USDC',
                            status='PENDING'))


def test_fuse_oids_default_none(store):
    repo = GridRepository(store)
    g = _new_grid(repo)
    assert g.fuse_low_oid is None and g.fuse_high_oid is None


def test_set_fuse_oids_persists(store):
    repo = GridRepository(store)
    g = _new_grid(repo)
    repo.set_fuse_oids(g.id, low_oid='OID_LOW', high_oid='OID_HIGH')
    g2 = repo.get(g.id)
    assert g2.fuse_low_oid == 'OID_LOW'
    assert g2.fuse_high_oid == 'OID_HIGH'


def test_set_fuse_oids_partial_leaves_other(store):
    repo = GridRepository(store)
    g = _new_grid(repo)
    repo.set_fuse_oids(g.id, low_oid='OID_LOW', high_oid='OID_HIGH')
    repo.set_fuse_oids(g.id, low_oid='NEW_LOW')          # 只更新 low
    g2 = repo.get(g.id)
    assert g2.fuse_low_oid == 'NEW_LOW'
    assert g2.fuse_high_oid == 'OID_HIGH'                 # high 不动


def test_set_fuse_oids_does_not_bump_version(store):
    repo = GridRepository(store)
    g = _new_grid(repo)
    repo.set_fuse_oids(g.id, low_oid='OID_LOW')
    assert repo.get(g.id).version == g.version            # 元数据更新不动乐观锁
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_grid_fuse_oids.py -v`
Expected: FAIL（`Grid` 无 `fuse_low_oid` / `set_fuse_oids` 不存在）。

- [ ] **Step 3: models.py 加列与字段**

`gridtrade/state/models.py`，`grids` Table 在 `version` 列之后（保持现有列顺序）加：
```python
    Column('fuse_low_oid', String, nullable=True),
    Column('fuse_high_oid', String, nullable=True),
```
`Grid` 数据类末尾（`version: int = 1` 之后）加：
```python
    fuse_low_oid: Optional[str] = None
    fuse_high_oid: Optional[str] = None
```

- [ ] **Step 4: grids.py 加字段映射与 set_fuse_oids**

`gridtrade/state/grids.py`：
1. `_FIELDS` 末尾加 `'fuse_low_oid', 'fuse_high_oid'`（即在 `'version'` 之后）。
2. 文件顶部 import 之后加哨兵：`_UNSET = object()`。
3. 加方法（类内）：
```python
    def set_fuse_oids(self, grid_id, *, low_oid=_UNSET, high_oid=_UNSET) -> None:
        vals = {}
        if low_oid is not _UNSET:
            vals['fuse_low_oid'] = low_oid
        if high_oid is not _UNSET:
            vals['fuse_high_oid'] = high_oid
        if not vals:
            return
        vals['updated_at'] = now_ms()
        with self.engine.begin() as c:
            c.execute(update(grids).where(grids.c.id == grid_id).values(**vals))
```

- [ ] **Step 5: dbadmin 迁移**

`gridtrade/runtime/dbadmin.py`，加函数并入 `migrate`：
```python
def add_grids_fuse_oids(store) -> str:
    """幂等：grids 缺 fuse_low_oid/fuse_high_oid 列则加上（NULL 允许）。"""
    cols = {c['name'] for c in sa.inspect(store.engine).get_columns('grids')}
    todo = [c for c in ('fuse_low_oid', 'fuse_high_oid') if c not in cols]
    if not todo:
        return 'skipped'
    with store.engine.begin() as c:
        for col in todo:
            c.execute(sa.text('ALTER TABLE grids ADD COLUMN %s VARCHAR' % col))
    return 'added'
```
`migrate` 返回列表追加一项：
```python
def migrate(store) -> list:
    return [('add_grid_fills_fee', add_grid_fills_fee(store)),
            ('add_grids_fuse_oids', add_grids_fuse_oids(store))]
```

- [ ] **Step 6: 跑测试确认通过 + 双后端**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/state/test_grid_fuse_oids.py -v`
Expected: PASS（4 个）。
（若本地有 PG：`export TEST_DATABASE_URL=postgresql://postgres:grid@localhost:5432/gridtrade` 再跑一遍，PG 也绿。）

- [ ] **Step 7: 迁移幂等性手测**

Run:
```bash
TZ=Asia/Shanghai .venv/bin/python -c "
from gridtrade.state.store import StateStore
from gridtrade.runtime import dbadmin
s = StateStore.in_memory(); s.create_all()
print(dbadmin.add_grids_fuse_oids(s))  # skipped（create_all 已建列）
"
```
Expected: 打印 `skipped`（新建库已含列，迁移幂等不报错）。

- [ ] **Step 8: Commit**

```bash
git add gridtrade/state/models.py gridtrade/state/grids.py \
        gridtrade/runtime/dbadmin.py tests/state/test_grid_fuse_oids.py
git commit -m "$(printf 'feat(state): grids 加 fuse_low_oid/fuse_high_oid + 迁移\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 3: 配置 — STOP_SLIPPAGE / STOP_ORDERS_ENABLED

**Files:**
- Modify: `gridtrade/config.py`（`DeployConfig` 两字段 + `load_deploy_config`）
- Test: `tests/runtime/test_config_stop.py`（新建）

**Interfaces:**
- Produces: `DeployConfig.stop_orders_enabled: bool = True`、`DeployConfig.stop_slippage: float = 0.15`，由 env `STOP_ORDERS_ENABLED`/`STOP_SLIPPAGE` 覆盖。

- [ ] **Step 1: 写失败测试**

`tests/runtime/test_config_stop.py`：
```python
from gridtrade.config import load_deploy_config


def test_stop_defaults():
    c = load_deploy_config(env={})
    assert c.stop_orders_enabled is True
    assert c.stop_slippage == 0.15


def test_stop_env_override():
    c = load_deploy_config(env={'STOP_ORDERS_ENABLED': 'false',
                                'STOP_SLIPPAGE': '0.2'})
    assert c.stop_orders_enabled is False
    assert c.stop_slippage == 0.2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_config_stop.py -v`
Expected: FAIL（`DeployConfig` 无 `stop_orders_enabled`）。

- [ ] **Step 3: 加配置**

`gridtrade/config.py`：
1. `DeployConfig` 加（与其他默认字段一起，dashboard 字段附近）：
```python
    stop_orders_enabled: bool = True
    stop_slippage: float = 0.15
```
2. `load_deploy_config` 的 `return DeployConfig(` 内加：
```python
        stop_orders_enabled=_b(env, 'STOP_ORDERS_ENABLED', True),
        stop_slippage=_f(env, 'STOP_SLIPPAGE', 0.15),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_config_stop.py -v`
Expected: PASS（2 个）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/config.py tests/runtime/test_config_stop.py
git commit -m "$(printf 'feat(config): STOP_ORDERS_ENABLED / STOP_SLIPPAGE\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 4: GridExecutor — 开网挂保险丝 / 关网撤保险丝 / restore 缓存

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`（`__init__`、`open`、`finalize_close`）
- Modify: `gridtrade/execution/reconciler.py`（`restore` 重建 `_fuses` 缓存）
- Test: `tests/execution/test_executor_fuses.py`（新建）

**Interfaces:**
- Consumes: Task 1 `adapter.create_stop_order`；Task 2 `grids.set_fuse_oids` + `Grid.fuse_low_oid/fuse_high_oid`；Task 3 配置。
- Produces:
  - `GridExecutor.__init__` 新增关键字参数 `stop_orders_enabled=False`、`stop_slippage=0.15`，存为 `self.stop_orders_enabled`/`self.stop_slippage`；新增 `self._fuses = {}`（grid_id -> {'low': oid, 'high': oid}）。
  - `open()` 在转 ACTIVE 前挂两张保险丝、写 `grids.fuse_*_oid`、填 `_fuses`。
  - `finalize_close()` 撤两张保险丝 oid（best-effort）。
  - `Reconciler.restore()` 重建 `_fuses[grid_id]`。

- [ ] **Step 1: 写失败测试**

`tests/execution/test_executor_fuses.py`：
```python
import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler

PARAMS = dict(low_price=90.0, high_price=110.0, grid_count=10,
              stop_low_price=80.0, stop_high_price=120.0)


def _executor(store, **kw):
    ex = FakeExchange()
    ex.set_price('BTC/USDC:USDC', 100.0)
    return GridExecutor(ex, store, cap=1000.0, leverage=5.0, **kw), ex


def test_open_places_two_fuses_when_enabled(store):
    ex, fake = _executor(store, stop_orders_enabled=True)
    gid = ex.open('hl', 'BTC/USDC:USDC', dict(PARAMS))
    stops = fake._stops['BTC/USDC:USDC']
    sides = sorted((s.side, s.price) for s in stops)
    assert sides == [('buy', 120.0), ('sell', 80.0)]      # buy@stop_high, sell@stop_low
    worst = ex.grids.get(gid).grid_count * ex.grids.get(gid).order_num
    assert all(s.size == worst for s in stops)
    g = ex.grids.get(gid)
    assert g.fuse_low_oid is not None and g.fuse_high_oid is not None
    assert ex._fuses[gid]['low'] == g.fuse_low_oid


def test_open_no_fuses_when_disabled(store):
    ex, fake = _executor(store, stop_orders_enabled=False)
    gid = ex.open('hl', 'BTC/USDC:USDC', dict(PARAMS))
    assert not fake._stops.get('BTC/USDC:USDC')
    g = ex.grids.get(gid)
    assert g.fuse_low_oid is None and g.fuse_high_oid is None


def test_close_cancels_surviving_fuses(store):
    ex, fake = _executor(store, stop_orders_enabled=True)
    gid = ex.open('hl', 'BTC/USDC:USDC', dict(PARAMS))
    ex.close(gid, 'BTC/USDC:USDC', '测试')
    assert not fake._stops.get('BTC/USDC:USDC')           # cancel_all 已清


def test_restore_rebuilds_fuse_cache(store):
    ex, fake = _executor(store, stop_orders_enabled=True)
    gid = ex.open('hl', 'BTC/USDC:USDC', dict(PARAMS))
    g = ex.grids.get(gid)
    ex._fuses.clear()                                      # 模拟新进程：内存态丢失
    Reconciler(ex).restore(gid)
    assert ex._fuses[gid]['low'] == g.fuse_low_oid
    assert ex._fuses[gid]['high'] == g.fuse_high_oid
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_executor_fuses.py -v`
Expected: FAIL（`GridExecutor` 无 `stop_orders_enabled` 参数 / `_fuses`）。

- [ ] **Step 3: GridExecutor.__init__ 加参数与状态**

`gridtrade/execution/grid_executor.py` 的 `__init__` 签名改为：
```python
    def __init__(self, adapter, store, *, cap, leverage, fee=0.0002,
                 c_rate_taker=0.0005, max_rate=0.68, min_amount=0.0,
                 stop_orders_enabled=False, stop_slippage=0.15):
```
方法体内（其他 `self._...` 旁）加：
```python
        self.stop_orders_enabled = bool(stop_orders_enabled)
        self.stop_slippage = float(stop_slippage)
        self._fuses = {}      # grid_id -> {'low': exchange_oid, 'high': exchange_oid}
```

- [ ] **Step 4: open() 挂保险丝**

`gridtrade/execution/grid_executor.py` 的 `open()` 内，**在逐线挂限价单的 for 循环之后、`g2 = self.grids.get(gid)` 之前**插入：
```python
        # 灾难保险丝：两张 reduce-only 触发市价单，破网价触发（reduce_only 封顶到真实仓）。
        # exchange order id 持久化到 grids 行，供跨重启对账判定已触发。
        if self.stop_orders_enabled:
            worst = order_num * int(grid_params['grid_count'])
            low = self.adapter.create_stop_order(
                symbol, 'sell', worst, grid_params['stop_low_price'],
                reduce_only=True, slippage=self.stop_slippage,
                client_oid='%s:fuse:low' % gid)
            high = self.adapter.create_stop_order(
                symbol, 'buy', worst, grid_params['stop_high_price'],
                reduce_only=True, slippage=self.stop_slippage,
                client_oid='%s:fuse:high' % gid)
            self.grids.set_fuse_oids(gid, low_oid=getattr(low, 'id', None),
                                     high_oid=getattr(high, 'id', None))
            self._fuses[gid] = {'low': getattr(low, 'id', None),
                                'high': getattr(high, 'id', None)}
```

- [ ] **Step 5: finalize_close() 撤保险丝**

`gridtrade/execution/grid_executor.py` 的 `finalize_close()` 内，`self.adapter.cancel_all(symbol)` 之后插入：
```python
        # 撤掉未触发的另一张保险丝（cancel_all 在多数所已覆盖触发单，这里再 best-effort 补刀，跨所稳妥）。
        for oid in (grid.fuse_low_oid, grid.fuse_high_oid):
            if oid:
                try:
                    self.adapter.cancel_order(symbol, oid)
                except Exception:
                    pass
```
（`grid` 已在方法首行 `grid = self.grids.get(grid_id)` 取到。）

- [ ] **Step 6: Reconciler.restore 重建缓存**

`gridtrade/execution/reconciler.py` 的 `restore()` 内，末尾（设置 `ex._funding_cursor[grid_id]` 之后）加：
```python
        ex._fuses[grid_id] = {'low': g.fuse_low_oid, 'high': g.fuse_high_oid}
```

- [ ] **Step 7: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_executor_fuses.py -v`
Expected: PASS（4 个）。

- [ ] **Step 8: 跑全套确认无回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿（既有测试构造 `GridExecutor` 不传新参 → `stop_orders_enabled=False` → 零行为变化）。

- [ ] **Step 9: Commit**

```bash
git add gridtrade/execution/grid_executor.py gridtrade/execution/reconciler.py \
        tests/execution/test_executor_fuses.py
git commit -m "$(printf 'feat(execution): 开网挂保险丝 + 关网撤保险丝 + restore 缓存\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 5: Reconciler.reconcile_fuses — 在挂/被丢/已触发三态判定

**Files:**
- Modify: `gridtrade/execution/reconciler.py`（加 `reconcile_fuses`、`_fuse_filled`）
- Modify: `gridtrade/runtime/cycles.py`（monitor 轮调 `reconcile_fuses`）
- Test: `tests/execution/test_reconcile_fuses.py`（新建）

**Interfaces:**
- Consumes: Task 4 的 `_fuses`/`grids.fuse_*_oid`/`stop_orders_enabled`；Task 1 的 `create_stop_order`。
- Produces:
  - `Reconciler.reconcile_fuses(self, grid_id, symbol) -> dict`，返回 `{'replaced': int, 'fired': bool}`。
    - 保险丝在挂 → 无动作；被丢（不在挂单簿且无成交）→ 重挂并回写 `grids.fuse_*_oid`；已触发（不在挂单簿且有成交）→ `executor.close(grid_id, symbol, '保险丝触发')`，`fired=True`。
    - `stop_orders_enabled=False` 时直接返回 `{'replaced': 0, 'fired': False}`。

- [ ] **Step 1: 写失败测试**

`tests/execution/test_reconcile_fuses.py`：
```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import CLOSED

SYM = 'BTC/USDC:USDC'
PARAMS = dict(low_price=90.0, high_price=110.0, grid_count=10,
              stop_low_price=80.0, stop_high_price=120.0)


def _open(store):
    fake = FakeExchange()
    fake.set_price(SYM, 100.0)
    ex = GridExecutor(fake, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=True)
    gid = ex.open('hl', SYM, dict(PARAMS))
    return ex, fake, gid


def test_fuses_in_book_no_action(store):
    ex, fake, gid = _open(store)
    rec = Reconciler(ex)
    out = rec.reconcile_fuses(gid, SYM)
    assert out == {'replaced': 0, 'fired': False}


def test_fired_fuse_tears_down_grid(store):
    ex, fake, gid = _open(store)
    rec = Reconciler(ex)
    fake.set_price(SYM, 79.0)               # 穿破 stop_low -> sell 保险丝触发、平多
    out = rec.reconcile_fuses(gid, SYM)
    assert out['fired'] is True
    assert ex.grids.get(gid).status == CLOSED
    assert not fake.fetch_open_orders(SYM)  # 撑网全拆，网格限价单全撤


def test_dropped_fuse_replaced_not_closed(store):
    ex, fake, gid = _open(store)
    g = ex.grids.get(gid)
    fake._stops[SYM] = [s for s in fake._stops[SYM]
                        if s.id != g.fuse_low_oid]   # 模拟 low 保险丝被交易所丢、无成交
    rec = Reconciler(ex)
    out = rec.reconcile_fuses(gid, SYM)
    assert out['fired'] is False
    assert out['replaced'] == 1
    assert ex.grids.get(gid).status != CLOSED
    new_low = ex.grids.get(gid).fuse_low_oid
    assert new_low != g.fuse_low_oid                 # 回写了新 oid
    assert any(s.id == new_low for s in fake._stops[SYM])


def test_disabled_short_circuits(store):
    fake = FakeExchange(); fake.set_price(SYM, 100.0)
    ex = GridExecutor(fake, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=False)
    gid = ex.open('hl', SYM, dict(PARAMS))
    out = Reconciler(ex).reconcile_fuses(gid, SYM)
    assert out == {'replaced': 0, 'fired': False}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_reconcile_fuses.py -v`
Expected: FAIL（`Reconciler` 无 `reconcile_fuses`）。

- [ ] **Step 3: 实现 reconcile_fuses + _fuse_filled**

`gridtrade/execution/reconciler.py`，`Reconciler` 类内加（`check_position_drift` 之后）：
```python
    def _fuse_filled(self, symbol, oid, since_ms):
        if oid is None:
            return False
        return any(t.order_id == oid
                   for t in self.ex.adapter.fetch_my_trades(symbol, since_ms=since_ms))

    def reconcile_fuses(self, grid_id, symbol):
        """灾难保险丝三态对账：在挂→无动作；被丢→重挂；已触发→撑网全拆。"""
        ex = self.ex
        if not ex.stop_orders_enabled:
            return {'replaced': 0, 'fired': False}
        g = ex.grids.get(grid_id)
        on_exchange = {o.id for o in ex.adapter.fetch_open_orders(symbol)}
        specs = [('low', 'sell', g.stop_low_price, g.fuse_low_oid),
                 ('high', 'buy', g.stop_high_price, g.fuse_high_oid)]
        replaced = 0
        for key, side, trigger, oid in specs:
            if oid is not None and oid in on_exchange:
                continue                                   # 在挂
            if self._fuse_filled(symbol, oid, g.created_at):
                ex.close(grid_id, symbol, '保险丝触发')   # 已触发 -> 撑网全拆
                return {'replaced': replaced, 'fired': True}
            # 被丢（或迁移空 oid）-> (重)挂，回写新 oid
            worst = float(g.grid_count) * float(g.order_num)
            order = ex.adapter.create_stop_order(
                symbol, side, worst, trigger, reduce_only=True,
                slippage=ex.stop_slippage, client_oid='%s:fuse:%s' % (grid_id, key))
            new_oid = getattr(order, 'id', None)
            ex.grids.set_fuse_oids(grid_id, **{'%s_oid' % key: new_oid})
            ex._fuses.setdefault(grid_id, {})[key] = new_oid
            replaced += 1
        return {'replaced': replaced, 'fired': False}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_reconcile_fuses.py -v`
Expected: PASS（4 个）。

- [ ] **Step 5: 接入 monitor 循环**

`gridtrade/runtime/cycles.py` 的 `run_monitor_cycle`，在对账 for 循环里 `reconciler.check_position_drift(...)` 之后、同一 `try` 块内加：
```python
                fuse = reconciler.reconcile_fuses(grid.id, grid.symbol)   # 保险丝三态
                if fuse.get('fired'):
                    log('[monitor] grid %s fuse fired -> grid closed' % grid.id)
```

- [ ] **Step 6: 跑全套确认无回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿。

- [ ] **Step 7: Commit**

```bash
git add gridtrade/execution/reconciler.py gridtrade/runtime/cycles.py \
        tests/execution/test_reconcile_fuses.py
git commit -m "$(printf 'feat(execution): reconcile_fuses 三态对账 + 接入 monitor 轮\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 6: factory 接线 — 把开关/滑点接进 executor

**Files:**
- Modify: `gridtrade/runtime/factory.py`（`GridExecutor(...)` 构造）
- Test: `tests/runtime/test_factory_stop.py`（新建）

**Interfaces:**
- Consumes: Task 3 配置、Task 4 executor 参数。
- Produces: `build_runtime(config)` 构造的 executor 携带 `config.stop_orders_enabled`/`config.stop_slippage`。

- [ ] **Step 1: 写失败测试**

`tests/runtime/test_factory_stop.py`：
```python
from gridtrade.config import load_deploy_config
from gridtrade.runtime.factory import build_runtime


def test_factory_threads_stop_config():
    cfg = load_deploy_config(env={
        'EXCHANGE': 'fake', 'STOP_ORDERS_ENABLED': 'true', 'STOP_SLIPPAGE': '0.2'})
    rt = build_runtime(cfg)
    assert rt.manager.executor.stop_orders_enabled is True
    assert rt.manager.executor.stop_slippage == 0.2
```
> 注：若 `build_adapter` 不支持 `EXCHANGE=fake`，改用现有测试构造 runtime 的既定方式（参考 `tests/runtime/` 里已有的 factory 测试如何提供 adapter）；断言不变。

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_factory_stop.py -v`
Expected: FAIL（executor 的 `stop_orders_enabled` 为默认 False）。

- [ ] **Step 3: factory 传参**

`gridtrade/runtime/factory.py` 的 `GridExecutor(...)` 构造改为：
```python
    executor = GridExecutor(adapter, store, cap=config.cap,
                            leverage=config.leverage,
                            stop_orders_enabled=config.stop_orders_enabled,
                            stop_slippage=config.stop_slippage)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_factory_stop.py -v`
Expected: PASS。

- [ ] **Step 5: 跑全套 + 提交**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全绿。
```bash
git add gridtrade/runtime/factory.py tests/runtime/test_factory_stop.py
git commit -m "$(printf 'feat(runtime): factory 把 stop 配置接进 executor\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Task 7: 收尾 — STATUS.md + DEPLOY.md + 记忆更新

**Files:**
- Modify: `docs/STATUS.md`（§5 部署加保险丝一段 + §9 删「原生止损」待办或标✅）
- Modify: `deploy/DEPLOY.md`（迁移命令加 `migrate` 已含 fuse 列；新增 env `STOP_ORDERS_ENABLED`/`STOP_SLIPPAGE`）
- Modify: `.env.example`（加两个 env）

**这是文档任务，无测试。**

- [ ] **Step 1: 更新 STATUS.md**

§5 部署段加一句：原生止损保险丝（reduce-only 触发单，破网价触发，`STOP_ORDERS_ENABLED`/`STOP_SLIPPAGE`，对账重挂 + 触发撑网全拆）；上线对存量库跑 `dbadmin migrate` 加 `fuse_low_oid/fuse_high_oid` 列。§9 待办里「交易所原生止损单」标 ✅ 或移除。

- [ ] **Step 2: 更新 DEPLOY.md + .env.example**

DEPLOY.md ops 清单加 `STOP_ORDERS_ENABLED`/`STOP_SLIPPAGE` 说明与迁移提醒；`.env.example` 加：
```
STOP_ORDERS_ENABLED=true
STOP_SLIPPAGE=0.15
```

- [ ] **Step 3: Commit**

```bash
git add docs/STATUS.md deploy/DEPLOY.md .env.example
git commit -m "$(printf 'docs(stop): 保险丝上线说明 + 迁移 + env\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

- [ ] **Step 4: 更新记忆**

更新 `hl-testnet-deploy-state` 记忆：原生止损保险丝已实现（待 testnet 验证 reduce-only 封顶 + HL 触发价类型 mark/last + 端到端破网触发）。在 MEMORY.md 对应行补一句。

---

## testnet 验证清单（实现后、mainnet 前；见 spec §7）

实现完成、合并后，部署到 testnet 实测（这些离线测试覆盖不到）：
1. 部署前跑 `fly machine run <image> python -m gridtrade.runtime.dbadmin migrate`。
2. HL testnet 实测 reduce-only 超额 size 是否封顶到持仓（spec §5.3 假设）；若不封顶 → 回退「按 net_size 每轮同步」。
3. HL 触发单默认对 mark / last 触发（`describe()` 里 `triggerPriceType` 默认 None）—— 必要时显式传。
4. 人为把网格区间设窄，观察价格穿破网价 → 保险丝触发 → reconcile_fuses 判已触发 → 撑网全拆。
5. 确认 HL `cancel_all` 是否覆盖触发单；若不覆盖，finalize_close 的 best-effort `cancel_order(fuse_oid)` 兜底是否生效。

---

## Self-Review 记录

- **Spec 覆盖**：§5.1 端口→T1；§5.2 Fake→T1；§5.3 开网挂+持久化→T2(列)+T4(挂)；§5.4 reconcile 三态→T5；§5.5 收尾→T4(撤)+T5(close)；§5.6 配置→T3+T6；§6 测试→各 Task 内嵌；§7 testnet→末节清单。无遗漏。
- **占位符**：无 TBD/TODO；每步含完整代码或精确命令。
- **类型一致**：`create_stop_order` 签名 base/ccxt/fake/resilient 一致；`set_fuse_oids(low_oid=/high_oid=)` 与 reconcile 的 `**{'%s_oid'%key:...}` 一致；`reconcile_fuses` 返回 `{'replaced','fired'}` 与测试断言一致；`_fuses[gid]={'low','high'}` 键在 open/restore/reconcile 三处一致。
