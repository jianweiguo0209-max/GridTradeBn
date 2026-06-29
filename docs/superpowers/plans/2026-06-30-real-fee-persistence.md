# 真实平台手续费落库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把每笔成交的真实平台手续费（`t.fee`）落库到 `grid_fills`，并让 `accounting.fee_paid` 与 `net_value/pnl_ratio` 改用真实费，而不改动共用回测引擎。

**Architecture:** 在 `grid_fills` 表加 `fee` 列、逐笔持久化交易所回报费；`LiveEquity` 累加真实费 `real_fee_paid`，`snapshot()` 用它替换估算 `fee_paid` 并按 `(est_fee - real_fee)/cap` 修正 `net_value`；`cal_equity_curve`（回测/实盘共用引擎）零改动。运行态与重启重放都把真实费喂进累加器，`accounting.fee_paid` 经既有 `acc.fee_paid = snap['fee_paid']` 自动转真实。表迁移作为 `dbadmin.py` 的新一次性 action。

**Tech Stack:** Python, SQLAlchemy（`metadata` 声明式建表，无 Alembic），pandas（引擎），pytest（`store` fixture 双模式 SQLite/PG，见 `tests/conftest.py`）。

## Global Constraints

- **不改 `gridtrade/core/grid_engine.py` 的 `cal_equity_curve`**（回测/实盘同源，行为必须不变）。
- 新增 DB 列：`grid_fills.fee`，类型 `Float`，`nullable=False`，`default=0.0`。
- 不回填历史 fee：历史 fill 的 `fee` 保持 0；只对上线后新成交记录真实费。
- 向后兼容：`LiveEquity.record_fill` / `replay` 不传 fee 时回退到估算费率 `size*price*self.fee`，保证既有调用与既有测试不变；合成底仓沿用此回退。
- 测试遵循既有 `store` fixture；测试文件已有的 setup helper（`_setup` / `_new_executor` 等）须复用、不要另造布网逻辑。
- TDD：每个 Task 先写失败测试再实现；每个 Task 末尾提交。
- 提交信息结尾附：`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

### Task 1: grid_fills 加 fee 列（模型 + dataclass + 仓储）

**Files:**
- Modify: `gridtrade/state/models.py`（`grid_fills` 表定义 + `Fill` dataclass）
- Modify: `gridtrade/state/fills.py:9`（`_FIELDS`）
- Test: `tests/state/test_fills.py`

**Interfaces:**
- Consumes: 无。
- Produces: `Fill` dataclass 新增字段 `fee: float = 0.0`（位于 `size` 之后、`ts` 之前，`ts` 改带默认值 `0`）；`grid_fills` 表新增 `fee` 列；`FillRepository.add_if_new(fill)` 持久化 `fee`、`list_by_grid(grid_id)` 返回的 `Fill.fee` 为持久化值（旧行默认 0.0）。

- [ ] **Step 1: 写失败测试**

在 `tests/state/test_fills.py` 末尾追加：

```python
def test_fee_persisted_and_read_back(store):
    repo = _repo(store)
    assert repo.add_if_new(_fill('tf', fee=1.23)) is True
    got = repo.list_by_grid('g1')
    assert len(got) == 1
    assert abs(got[0].fee - 1.23) < 1e-12


def test_fee_defaults_zero_when_omitted(store):
    repo = _repo(store)
    repo.add_if_new(_fill('t0'))          # _fill 不传 fee → Fill.fee 默认 0.0
    assert repo.list_by_grid('g1')[0].fee == 0.0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/state/test_fills.py::test_fee_persisted_and_read_back -v`
Expected: FAIL（`TypeError: __init__() got an unexpected keyword argument 'fee'`）

- [ ] **Step 3: 实现 —— 表列 + dataclass + 仓储字段**

`gridtrade/state/models.py` 的 `grid_fills` 表，在 `size` 列之后加一列：

```python
grid_fills = Table(
    'grid_fills', metadata,
    Column('trade_id', String, primary_key=True),
    Column('grid_id', String, nullable=False),
    Column('line_index', Integer, nullable=False),
    Column('side', String, nullable=False),
    Column('price', Float, nullable=False),
    Column('size', Float, nullable=False),
    Column('fee', Float, nullable=False, default=0.0),
    Column('ts', BigInteger, nullable=False),
    Column('created_at', BigInteger, nullable=False),
    Index('ix_grid_fills_grid', 'grid_id'),
)
```

同文件 `Fill` dataclass（顺序与表列一致；因在 `ts` 前插入带默认值的 `fee`，`ts` 也要给默认值）：

```python
@dataclass
class Fill:
    trade_id: str
    grid_id: str
    line_index: int
    side: str
    price: float
    size: float
    fee: float = 0.0
    ts: int = 0
    created_at: int = 0
```

> 现有构造点都用关键字传 `ts=...`（`grid_executor.py:120`、`tests/state/test_fills.py` 的 `_fill`），不受 `ts` 默认值化影响。

`gridtrade/state/fills.py` 的字段元组加入 `fee`（顺序与表列一致）：

```python
_FIELDS = ('trade_id', 'grid_id', 'line_index', 'side', 'price', 'size', 'fee', 'ts', 'created_at')
```

- [ ] **Step 4: 运行测试，确认通过（含既有 fills 测试不回归）**

Run: `pytest tests/state/test_fills.py -v`
Expected: PASS（新增 2 条 + 既有 3 条全绿）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/state/models.py gridtrade/state/fills.py tests/state/test_fills.py
git commit -m "feat(fee): grid_fills 加 fee 列 + Fill/仓储字段（落库基础）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: LiveEquity 真实费汇总 + net_value 修正

**Files:**
- Modify: `gridtrade/execution/live_equity.py`（`__init__` / `record_fill` / `replay` / `snapshot`）
- Test: `tests/execution/test_live_equity.py`

**Interfaces:**
- Consumes: 无（纯内存逻辑）。
- Produces:
  - `LiveEquity.real_fee_paid: float`（累计真实费）。
  - `record_fill(price, side, size, ts_ms, fee=None)`：`fee=None` → 回退 `size*price*self.fee`，否则用真实费；累加到 `real_fee_paid`。
  - `replay(fills)`：接受 `(price, side, size, ts_ms)` 或 `(price, side, size, ts_ms, fee)`（向后兼容）。
  - `snapshot(mark_price)['fee_paid']` = `real_fee_paid`；`net_value/pnl_ratio` 按 `(est_fee - real_fee)/cap` 修正。

- [ ] **Step 1: 写失败测试**

在 `tests/execution/test_live_equity.py` 末尾追加（沿用文件顶部 `CAP=1000.0`、`FEE=0.0002`、`_le()`）：

```python
def test_snapshot_fee_paid_is_real_sum():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000, fee=0.7)
    le.record_fill(99.0, 'sell', 0.5, 120_000, fee=0.9)
    snap = le.snapshot(100.0)
    assert abs(snap['fee_paid'] - 1.6) < 1e-12      # 0.7 + 0.9


def test_net_value_corrected_to_real_fee():
    fills_geom = [(99.0, 'buy', 0.5, 60_000), (98.0, 'buy', 0.5, 120_000)]
    est = _le(entry=100.0)
    for p, s, sz, ts in fills_geom:
        est.record_fill(p, s, sz, ts)               # fee=None → 估算费率
    est_snap = est.snapshot(100.0)

    real = _le(entry=100.0)
    for p, s, sz, ts in fills_geom:
        real.record_fill(p, s, sz, ts, fee=3.0)     # 每笔真实费 3.0，共 6.0
    real_snap = real.snapshot(100.0)

    assert real_snap['fee_paid'] == 6.0
    # net_value 用真实费替换估算费：real = est + (est_fee - real_fee)/cap
    expected = est_snap['net_value'] + (est_snap['fee_paid'] - 6.0) / CAP
    assert abs(real_snap['net_value'] - expected) < 1e-12
    assert abs(real_snap['pnl_ratio'] - (real_snap['net_value'] - 1.0)) < 1e-12


def test_replay_accepts_fee_tuples():
    fills = [(99.0, 'buy', 0.5, 60_000, 0.4), (98.0, 'buy', 0.5, 120_000, 0.6)]
    rep = _le(entry=100.0).replay(fills)
    assert abs(rep.snapshot(100.0)['fee_paid'] - 1.0) < 1e-12
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest "tests/execution/test_live_equity.py::test_snapshot_fee_paid_is_real_sum" "tests/execution/test_live_equity.py::test_net_value_corrected_to_real_fee" "tests/execution/test_live_equity.py::test_replay_accepts_fee_tuples" -v`
Expected: FAIL（`record_fill` 不接受 `fee` / `replay` 解包 5 元组报错 / `fee_paid` 仍是估算值）

- [ ] **Step 3: 实现**

`gridtrade/execution/live_equity.py`：

`__init__` 末尾加累加器：

```python
        self.funding_paid = 0.0
        self.real_fee_paid = 0.0
```

`record_fill` 加 `fee` 参数并累加（几何逻辑不变）：

```python
    def record_fill(self, price, side, size, ts_ms, fee=None):
        if side not in ('buy', 'sell'):
            raise ValueError("side must be 'buy' or 'sell'")
        # fee=None：无真实费时回退估算费率（与共用引擎口径一致），保持向后兼容
        real_fee = float(size) * float(price) * self.fee if fee is None else float(fee)
        self.real_fee_paid += real_fee
        order_dir = 1.0 if side == 'buy' else -1.0
        if self._fills:
            last_touch = self._fills[-1]['touch']
        elif self.entry_price is not None:
            last_touch = self.entry_price
        else:
            last_touch = float(price)
        self._fills.append({
            'candle_begin_time': pd.to_datetime(int(ts_ms), unit='ms'),
            'last_touch': float(last_touch), 'touch': float(price),
            'order_dir': order_dir, 'order_num': float(size),
        })
        self._last_ts = int(ts_ms)
```

`replay` 兼容 4/5 元组：

```python
    def replay(self, fills) -> 'LiveEquity':
        """fills: 可迭代的 (price, side, size, ts_ms) 或 (price, side, size, ts_ms, fee)。
        供 reconciler 从持久化成交重建。"""
        for price, side, size, ts_ms, *rest in fills:
            fee = rest[0] if rest else None
            self.record_fill(price, side, size, ts_ms, fee)
        return self
```

`snapshot` 用真实费替换 `fee_paid` 并修正 `net_value`（空 fills 分支已返回 `fee_paid` 0.0，无需改）：

```python
    def snapshot(self, mark_price) -> dict:
        """Mark-to-market snapshot: net_value/fee_paid via cal_equity_curve WITHOUT _apply_exit,
        so excludes close-out taker fee (applied by executor on actual exit).
        fee_paid 取真实累计费（real_fee_paid），net_value 按 (est_fee - real_fee)/cap 修正。"""
        if not self._fills:
            return {'net_value': 1.0, 'pnl_ratio': 0.0, 'net_position': 0.0,
                    'avg_price': 0.0, 'realized_pnl': 0.0, 'fee_paid': 0.0,
                    'funding_paid': self.funding_paid}
        trade_df = pd.DataFrame(self._fills)
        mark_ts = pd.to_datetime(self._last_ts + 60_000, unit='ms')  # 严格晚于所有成交
        mp = float(mark_price)
        candle_df = pd.DataFrame([{
            'candle_begin_time': mark_ts, 'open': mp, 'high': mp, 'low': mp,
            'close': mp, 'symbol': '_LIVE_',
        }])
        eq = cal_equity_curve(candle_df, trade_df.copy(), self.fee, self.cap,
                              self.c_rate_taker, funding_df=None)
        last = eq.iloc[-1]
        est_fee = float(last['fee'])                 # 引擎按费率估算的累计费
        # net_value 内已扣 est_fee；用真实费替换：+est_fee/cap -real_fee/cap -funding/cap
        net_value = (float(last['net_value'])
                     + (est_fee - self.real_fee_paid) / self.cap
                     - self.funding_paid / self.cap)
        return {'net_value': net_value, 'pnl_ratio': net_value - 1.0,
                'net_position': float(last['hold_num']), 'avg_price': float(last['avg_price']),
                'realized_pnl': float(last['real_profit']), 'fee_paid': self.real_fee_paid,
                'funding_paid': self.funding_paid}
```

- [ ] **Step 4: 运行测试，确认通过（含既有 live_equity 测试不回归）**

Run: `pytest tests/execution/test_live_equity.py -v`
Expected: PASS。既有 `test_snapshot_matches_full_path_engine` 等不传 fee → `real_fee_paid == est_fee` → 修正项为 0 → 仍与全路径引擎一致；`test_replay_matches_incremental` 的 4 元组经 `*rest` 兼容仍绿。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/live_equity.py tests/execution/test_live_equity.py
git commit -m "feat(fee): LiveEquity 汇总真实费 + 修正 net_value（引擎不动）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: grid_executor 把真实费接入持久化与运行态记账

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`（`sync()` 约 119-120 行的 `Fill` 构造 + 约 127 行的 `live.record_fill`）
- Test: `tests/execution/test_grid_executor.py`

**Interfaces:**
- Consumes: Task 1 `Fill(fee=...)`；Task 2 `record_fill(..., fee=...)` / `LiveEquity.real_fee_paid`。
- Produces: `sync()` 摄入成交时，`grid_fills.fee` = `t.fee`，且运行态 `self.live[grid_id].real_fee_paid` 累加真实 `t.fee`；既有 `acc.fee_paid = snap['fee_paid']`（约 156 行）自动写真实值。`open()` 合成底仓不变（估算回退）。

- [ ] **Step 1: 写失败测试**

`tests/execution/test_grid_executor.py` 复用文件顶部既有的 `_setup` / `SYM` / `GP` / `ex_exchange_name`（见文件 11-16、58-66 行的用法）。在末尾追加：

```python
def test_sync_wires_real_fee_into_persistence_and_accounting(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    fee_after_open = gx.live[gid].real_fee_paid       # 仅合成底仓（估算回退）
    ex.set_price(SYM, 100.6)                           # 触发 line 5 卖单成交
    res = gx.sync(gid, SYM)
    assert res['new_fills'] == 1

    f = gx.fills.list_by_grid(gid)[0]
    real_fill_fee = f.size * f.price * 0.0005          # FakeExchange 费率 0.0005
    # (a) 落库真实费
    assert abs(f.fee - real_fill_fee) < 1e-9
    # (b) 运行态 live 累加的是真实费（增量==真实费，而非 0.0002 估算回退）
    delta = gx.live[gid].real_fee_paid - fee_after_open
    assert abs(delta - real_fill_fee) < 1e-9
    # (c) accounting.fee_paid 已用真实快照
    acc = gx.accounting.get(gid)
    assert abs(acc.fee_paid - gx.live[gid].snapshot(ex.fetch_price(SYM))['fee_paid']) < 1e-9
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/execution/test_grid_executor.py::test_sync_wires_real_fee_into_persistence_and_accounting -v`
Expected: FAIL（`f.fee == 0.0`；且 `delta` 为 0.0002 估算值，≠ 0.0005 真实费）

- [ ] **Step 3: 实现 —— sync 两处带 t.fee**

`gridtrade/execution/grid_executor.py` 的 `sync()`：

构造 `Fill`（约 119-120 行）改为带 fee：

```python
            fill = Fill(trade_id=str(t.id), grid_id=grid_id, line_index=line_index,
                        side=t.side, price=float(t.price), size=float(t.size),
                        fee=float(t.fee), ts=int(t.ts))
```

喂运行态累加器（约 127 行）改为带 fee：

```python
            self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts, float(t.fee))
```

> `open()` 约 80 行的合成底仓 `self.live[gid].record_fill(entry, 'buy', order_num, 0)` **保持不变**（无真实成交，走估算回退）。

- [ ] **Step 4: 运行测试，确认通过（含既有 grid_executor 测试不回归）**

Run: `pytest tests/execution/test_grid_executor.py -v`
Expected: PASS（新增用例绿 + 既有用例不回归）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/grid_executor.py tests/execution/test_grid_executor.py
git commit -m "feat(fee): grid_executor sync 落库 t.fee 并接入运行态真实费记账

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: reconciler 重放带 fee，重启重建真实费不丢

**Files:**
- Modify: `gridtrade/execution/reconciler.py:31`（重放真实成交时传 `f.fee`）
- Test: `tests/execution/test_reconciler.py`

**Interfaces:**
- Consumes: Task 1 `Fill.fee`、Task 2 `record_fill(..., fee=...)`、Task 3（sync 落库 fee）。
- Produces: `restore(gid)` 重建后的 `ex.live[gid].real_fee_paid` 包含 `grid_fills.fee` 真实之和（合成底仓沿用估算回退，与运行态一致）。

- [ ] **Step 1: 写失败测试**

`tests/execution/test_reconciler.py` 复用文件顶部 `_new_executor` / `SYM` / `GP`（见 9-31 行的 restore 用法）。在末尾追加：

```python
def test_restore_rebuilds_real_fee_from_persisted_fills(store):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = _new_executor(ex, store)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 100.6); gx.sync(gid, SYM)        # 一笔成交，真实费落库（Task 3）
    fee_before = gx.live[gid].snapshot(ex.fetch_price(SYM))['fee_paid']
    assert fee_before > 0.0

    # 模拟重启：全新 executor，从持久化 grid_fills 重放
    gx2 = _new_executor(ex, store)
    from gridtrade.execution.reconciler import Reconciler
    Reconciler(gx2).restore(gid)
    fee_after = gx2.live[gid].snapshot(ex.fetch_price(SYM))['fee_paid']
    assert abs(fee_after - fee_before) < 1e-9          # 重放自持久化 fee，不丢、与运行态一致
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/execution/test_reconciler.py::test_restore_rebuilds_real_fee_from_persisted_fills -v`
Expected: FAIL（重放未传 fee → 真实成交走估算回退 0.0002，`fee_after` ≠ 运行态真实 0.0005 口径的 `fee_before`）

- [ ] **Step 3: 实现 —— 重放传 f.fee**

`gridtrade/execution/reconciler.py` 定位真实成交重放（约 30-31 行）：

```python
        for f in ex.fills.list_by_grid(grid_id):   # 已按 ts 升序
            live.record_fill(f.price, f.side, f.size, f.ts)
```

改为传入持久化真实费：

```python
        for f in ex.fills.list_by_grid(grid_id):   # 已按 ts 升序
            live.record_fill(f.price, f.side, f.size, f.ts, f.fee)
```

> 约 29 行合成底仓 `live.record_fill(g.entry_price, 'buy', order_num, 0)` **保持不变**（估算回退，与运行态 `open()` 对齐）。

- [ ] **Step 4: 运行测试，确认通过（含既有 reconciler 测试不回归）**

Run: `pytest tests/execution/test_reconciler.py -v`
Expected: PASS（新增用例绿 + 既有 restore/reconcile 用例不回归）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/reconciler.py tests/execution/test_reconciler.py
git commit -m "feat(fee): reconcile 重放持久化 f.fee，重启重建真实费不丢

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 一次性迁移 —— dbadmin migrate 加 fee 列

**Files:**
- Modify: `gridtrade/runtime/dbadmin.py`（新增 `add_grid_fills_fee` + `migrate` + `run('migrate')` 接线）
- Create: `tests/runtime/__init__.py`（若 `tests/runtime/` 不存在）
- Test: `tests/runtime/test_dbadmin_migrate.py`

**Interfaces:**
- Consumes: `StateStore`（`store.engine`）。
- Produces: `add_grid_fills_fee(store) -> str`（幂等：缺列 `ALTER TABLE grid_fills ADD COLUMN fee ... DEFAULT 0` 返回 `'added'`，有列返回 `'skipped'`）；`migrate(store) -> list`；CLI `python -m gridtrade.runtime.dbadmin migrate`。

- [ ] **Step 1: 写失败测试**

新建 `tests/runtime/__init__.py`（空）与 `tests/runtime/test_dbadmin_migrate.py`：

```python
import sqlalchemy as sa

from gridtrade.runtime.dbadmin import add_grid_fills_fee
from gridtrade.state.store import StateStore


def _table_without_fee(engine):
    """建一个不含 fee 列的 grid_fills（模拟迁移前旧库）。"""
    md = sa.MetaData()
    sa.Table(
        'grid_fills', md,
        sa.Column('trade_id', sa.String, primary_key=True),
        sa.Column('grid_id', sa.String, nullable=False),
        sa.Column('line_index', sa.Integer, nullable=False),
        sa.Column('side', sa.String, nullable=False),
        sa.Column('price', sa.Float, nullable=False),
        sa.Column('size', sa.Float, nullable=False),
        sa.Column('ts', sa.BigInteger, nullable=False),
        sa.Column('created_at', sa.BigInteger, nullable=False),
    )
    md.create_all(engine)


def _cols(engine):
    return {c['name'] for c in sa.inspect(engine).get_columns('grid_fills')}


def test_migrate_adds_fee_column():
    st = StateStore.in_memory()
    _table_without_fee(st.engine)
    assert 'fee' not in _cols(st.engine)
    assert add_grid_fills_fee(st) == 'added'
    assert 'fee' in _cols(st.engine)


def test_migrate_is_idempotent():
    st = StateStore.in_memory()
    st.create_all()                       # 新库已含 fee 列（Task 1 后）
    assert add_grid_fills_fee(st) == 'skipped'
    assert add_grid_fills_fee(st) == 'skipped'
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/runtime/test_dbadmin_migrate.py -v`
Expected: FAIL（`ImportError: cannot import name 'add_grid_fills_fee'`）

- [ ] **Step 3: 实现 —— dbadmin 幂等迁移 + migrate action**

把 `gridtrade/runtime/dbadmin.py` 整体改写为：

```python
"""DB 管理一次性入口：create / reset / migrate。在 fly 上用 `fly machine run <image> \
python -m gridtrade.runtime.dbadmin <action>` 跑一次。

- create：仅 create_all（幂等，安全）。
- reset：drop_all + create_all（**销毁所有表数据**，仅 testnet/无价值数据时用）。
- migrate：对已存在的库做增量迁移（幂等）。当前：grid_fills 加 fee 列。
"""
import sys

import sqlalchemy as sa

from gridtrade.config import load_deploy_config
from gridtrade.state.store import StateStore


def _store():
    cfg = load_deploy_config()
    return (StateStore.from_url(cfg.database_url) if cfg.database_url
            else StateStore.in_memory())


def add_grid_fills_fee(store) -> str:
    """幂等：grid_fills 缺 fee 列则加上（DEFAULT 0），有则跳过。返回 'added'/'skipped'。"""
    cols = {c['name'] for c in sa.inspect(store.engine).get_columns('grid_fills')}
    if 'fee' in cols:
        return 'skipped'
    with store.engine.begin() as c:
        c.execute(sa.text(
            'ALTER TABLE grid_fills ADD COLUMN fee DOUBLE PRECISION NOT NULL DEFAULT 0'))
    return 'added'


def migrate(store) -> list:
    """跑所有增量迁移（幂等）。返回每步结果。"""
    return [('add_grid_fills_fee', add_grid_fills_fee(store))]


def run(action, *, store_factory=None):
    store = store_factory() if store_factory else _store()
    if action == 'reset':
        store.drop_all()
        store.create_all()
        return 'reset'
    if action == 'create':
        store.create_all()
        return 'create'
    if action == 'migrate':
        return migrate(store)
    raise SystemExit('usage: python -m gridtrade.runtime.dbadmin [create|reset|migrate]')


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'create'
    result = run(action)
    print('[dbadmin] %s done: %s' % (action, result), flush=True)


if __name__ == '__main__':
    main()
```

> `DOUBLE PRECISION`：SQLite 接受任意列类型名（亲和性规则），PG 原生支持，同一条 DDL 两库通用。`NOT NULL DEFAULT 0` 给已有行回填 0（符合「不回填历史费」）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/runtime/test_dbadmin_migrate.py -v`
Expected: PASS（加列 + 幂等两条全绿）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/runtime/dbadmin.py tests/runtime/
git commit -m "feat(fee): dbadmin migrate —— grid_fills 加 fee 列（幂等一次性迁移）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 全量回归 + 文档收尾

**Files:**
- 仅运行测试 + 可选更新 `docs/STATUS.md`

- [ ] **Step 1: 全量测试**

Run: `pytest -q`
Expected: 全绿（无回归）。若配置了 `TEST_DATABASE_URL`，另跑一遍验证 PG 落库读回：`TEST_DATABASE_URL=... pytest tests/state/test_fills.py -q`。

- [ ] **Step 2: 更新 STATUS（若适用）**

若 `docs/STATUS.md` 有「数据库表/字段」或「待办」小节，补一行：`grid_fills.fee 已落库（真实平台手续费）；accounting.fee_paid/net_value 改用真实费；上线需 fly 跑 dbadmin migrate；历史 fee 不回填`。无相关小节则跳过本步。

- [ ] **Step 3: 提交（若有改动）**

```bash
git add docs/STATUS.md
git commit -m "docs(status): 记录真实手续费落库 + migrate 部署步骤

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 部署备注（实现后人工执行，不在本计划自动范围）

上线时对现有 testnet Postgres 跑一次迁移（加 fee 列）：

```
fly machine run <image> python -m gridtrade.runtime.dbadmin migrate
```

幂等，可重复跑。跑前已部署、正在运行的网格，重启重放历史 fill 的 `fee=0` → 该网格累计真实费会漏掉历史段（随新成交自愈），属「不回填」的已知接受代价。
