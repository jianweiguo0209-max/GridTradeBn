# 真中性网格改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把单一网格路径从「名中性实做多」（开网市价买 N_above 底仓）原地改造成真市场中性（开网即 flat；价涨转净空、价跌转净多），实盘与离线回测同口径。

**Architecture:** 外科式删除 `open()` 与 `restore()` 里对称的 init 底仓两段（二者必须同改，否则重启对账背离），并把回测入口切到 `neutral_init=False`。共用盈亏/退出数学与金标 parity 零改（审计已证净空下记账精确、止损/保险丝方向天然正确）。清场切换上线，无 schema 变更。

**Tech Stack:** Python 3.9 / pandas 1.3.5 / numpy 1.22.4 / SQLAlchemy 2.0 / pytest；交易所端口用 `FakeExchange` 离线撮合。

## Global Constraints

- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest`（仓库 `.venv`）。默认走内存 SQLite。
- 不新增第三方依赖；不改 `core/` 对交易所库的零依赖不变量。
- **不改**共用盈亏/退出数学：`cal_equity_curve` / `_apply_exit` / `evaluate_exit`。
- **不改** `simulate_grid_engine` 的 `neutral_init` 参数与默认值，**不改**金标 parity 测试 `tests/core/test_grid_engine_parity.py`。
- 无 schema 变更、无 DB migrate。
- 提交信息用中文 conventional commit，结尾附 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 分支：`feat/true-neutral-grid`（已建，spec 已提交在此分支）。

---

## File Structure

**生产改动（2 文件，各一处）：**
- `gridtrade/execution/grid_executor.py` — `open()` 去 init 底仓段。
- `gridtrade/execution/reconciler.py` — `restore()` 去 init 重放段（与 open 对称）。
- `gridtrade/backtest/backtest_run.py` — 回测入口 `neutral_init=False`。

**测试改动（改现有 + 新增）：**
- 改：`tests/execution/test_grid_executor.py`、`tests/execution/test_monitor.py`、`tests/execution/test_chaos_close.py`、`tests/runtime/test_cycles.py`。
- 删：`tests/execution/test_live_equity.py::test_neutral_init_base_inventory`。
- 新增：`tests/execution/test_neutral_accounting.py`、`tests/execution/test_neutral_fuse.py`。

**文档：** `docs/STATUS.md`（§7 开网描述）。

---

## Task 1: 开网即 flat（去 init，open + restore 对称）

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`（`open()`，约 82–88 行）
- Modify: `gridtrade/execution/reconciler.py`（`restore()`，约 30–35 行）
- Test: `tests/execution/test_grid_executor.py`、`tests/execution/test_monitor.py`、`tests/execution/test_chaos_close.py`、`tests/runtime/test_cycles.py`

**Interfaces:**
- Consumes: `GridExecutor.open(exchange, symbol, grid_params, *, offset=0, tag='', cap=None) -> gid`（签名不变）；`Reconciler(executor).restore(grid_id)`（签名不变）。
- Produces: 改造后 `open()` 返回的网格开网时 `adapter.fetch_positions(symbol).net_size == 0`；`restore()` 重建的 `LiveEquity` 仅由持久化 fills 组成（无幻影 init）。后续 Task 2/3 依赖此行为。

- [ ] **Step 1: 写表达目标行为的新测试（先红）**

在 `tests/execution/test_grid_executor.py` 末尾 `ex_exchange_name()` 之前，加入两个新测试，并把旧的 `test_open_places_grid_and_neutral_inventory` 整体替换为 `test_open_starts_flat`。

替换旧测试（`tests/execution/test_grid_executor.py` 第 19–35 行）：

```python
def test_open_starts_flat(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP, offset=0, tag='t0')
    from gridtrade.state.grids import GridRepository
    g = GridRepository(store).get(gid)
    assert g.status == ACTIVE and g.entry_price == 100.0
    # 真中性：开网即 flat，无初始市价单
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9
    # 9 条线，entry 不在线上 → 9 个挂单（4 sell / 5 buy）
    opens = ex.fetch_open_orders(SYM)
    assert len(opens) == 9
    sells = [o for o in opens if o.side == 'sell']
    buys = [o for o in opens if o.side == 'buy']
    assert len(sells) == 4 and len(buys) == 5
    # 无 :init: 市价成交
    assert all(':init:' not in t.client_oid for t in ex.fetch_my_trades(SYM))


def test_neutral_net_follows_price_short_above_long_below(store):
    ex, store, gx = _setup(store, price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    ex.set_price(SYM, 102.5); gx.sync(gid, SYM)   # 穿所有卖线 → 净空
    assert ex.fetch_positions(SYM).net_size < 0
    ex.set_price(SYM, 97.5); gx.sync(gid, SYM)    # 穿所有买线 → 净多
    assert ex.fetch_positions(SYM).net_size > 0
```

- [ ] **Step 2: 运行新测试，确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor.py::test_open_starts_flat tests/execution/test_grid_executor.py::test_neutral_net_follows_price_short_above_long_below -q`
Expected: FAIL —— `test_open_starts_flat` 因当前 `open()` 建 init 底仓，`net_size` ≈ `on*4` ≠ 0。

- [ ] **Step 3: 去掉 `open()` 的 init 底仓段**

`gridtrade/execution/grid_executor.py`，把第 82–88 行：

```python
        # 中性底仓：入场价上方线数 × 每格量，市价买
        above = [p for p in price_array if p > entry]
        if above:
            self.adapter.create_market_order(symbol, 'buy', order_num * len(above),
                                             client_oid='%s:init:0' % gid)
            for _ in range(len(above)):
                self.live[gid].record_fill(entry, 'buy', order_num, 0)
```

替换为：

```python
        # 真中性：开网不建底仓，净仓从 0 开始（价涨→挂单成交转净空，价跌→转净多）。
```

- [ ] **Step 4: 去掉 `restore()` 的 init 重放段（与 open 对称）**

`gridtrade/execution/reconciler.py`，把第 30–33 行：

```python
        live = LiveEquity(ex.cap, ex.fee, ex.c_rate_taker, entry_price=g.entry_price)
        above = [p for p in price_array if p > g.entry_price]
        for _ in range(len(above)):
            live.record_fill(g.entry_price, 'buy', order_num, 0)
        for f in ex.fills.list_by_grid(grid_id):   # 已按 ts 升序
```

替换为：

```python
        live = LiveEquity(ex.cap, ex.fee, ex.c_rate_taker, entry_price=g.entry_price)
        # 真中性：无 init 底仓（与 open 对称）；仅从持久化成交重建，否则重启后模型多出幻影多头。
        for f in ex.fills.list_by_grid(grid_id):   # 已按 ts 升序
```

- [ ] **Step 5: 运行新测试，确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor.py::test_open_starts_flat tests/execution/test_grid_executor.py::test_neutral_net_follows_price_short_above_long_below -q`
Expected: PASS（2 passed）。

- [ ] **Step 6: 跑全套定位被 init 移除打破的旧测试**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: FAIL 集中在这几处（其余全绿）：
- `test_grid_executor.py::test_sync_records_fill_and_replenishes`（断言 `on*3`）
- `test_monitor.py::test_monitor_triggers_fixed_stop_and_closes`（仅注释语义变，逻辑仍触发，通常不失败——若失败见 Step 8）
- `test_chaos_close.py` 三处 `net_size > 0`
- `test_cycles.py::test_monitor_cycle_resumes_stuck_closing_grid`（`net_size > 0`）

- [ ] **Step 7: 修 `test_sync_records_fill_and_replenishes`（净空口径）**

`tests/execution/test_grid_executor.py` 第 67–73 行：卖单成交后中性下净仓为 `-on`（原多头 `on*3`）。把

```python
    # LiveEquity 记录了该成交，净仓下降一格量
    from gridtrade.state.grids import GridRepository
    on = GridRepository(store).get(gid).order_num
    assert abs(ex.fetch_positions(SYM).net_size - on * 3) < 1e-6
    # accounting 落了快照
    acc = gx.accounting.get(gid)
    assert acc is not None and abs(acc.net_position - on * 3) < 1e-6
```

改为

```python
    # 真中性：开网 flat，一笔卖单成交 → 净空一格量（-on）
    from gridtrade.state.grids import GridRepository
    on = GridRepository(store).get(gid).order_num
    assert abs(ex.fetch_positions(SYM).net_size - (-on)) < 1e-6
    # accounting 落了快照
    acc = gx.accounting.get(gid)
    assert acc is not None and abs(acc.net_position - (-on)) < 1e-6
```

同文件第 173 行注释语义已变（无底仓），把

```python
    fee_after_open = gx.live[gid].real_fee_paid       # 仅合成底仓（估算回退）
```

改为

```python
    fee_after_open = gx.live[gid].real_fee_paid       # 真中性：开网 flat，无底仓费 → 0
```

- [ ] **Step 8: 修 `test_monitor.py` 注释（逻辑不变）**

`tests/execution/test_monitor.py` 第 31 行，把

```python
    # 价格大跌：中性底仓多头浮亏，pnl_ratio 跌破 -3.4% → 固定止损
```

改为

```python
    # 价格大跌：买线成交累出净多、深度浮亏，pnl_ratio 跌破 -3.4% → 固定止损
```

- [ ] **Step 9: 修 `test_chaos_close.py` 三处（先驱动成交累出净仓再平）**

`tests/execution/test_chaos_close.py`：三个测试都在 `open()` 后假设 `net_size > 0`。中性下开网 flat，需先驱动买线成交累出净多（保留「平掉真实仓」的验证意图）。

第 25–31 行 `test_close_clean_flattens_position_baseline`，把

```python
    fake, faulty, gx = build_stack(store)
    gid = gx.open('fake', SYM, GP)               # 中性底仓 -> 持有多头净仓
    assert fake.fetch_positions(SYM).net_size > 0
```

改为

```python
    fake, faulty, gx = build_stack(store)
    gid = gx.open('fake', SYM, GP)               # 真中性：开网 flat
    fake.set_price(SYM, 98.5); gx.sync(gid, SYM) # 驱动买线成交 → 累出净多
    assert fake.fetch_positions(SYM).net_size > 0
```

第 36–39 行 `test_close_partial_fill_is_flattened_by_bounded_retry`，把

```python
    fake, faulty, gx = build_stack(store)
    gid = gx.open('fake', SYM, GP)
    net_before = fake.fetch_positions(SYM).net_size
    assert net_before > 0
```

改为

```python
    fake, faulty, gx = build_stack(store)
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 98.5); gx.sync(gid, SYM) # 驱动买线成交 → 累出净多
    net_before = fake.fetch_positions(SYM).net_size
    assert net_before > 0
```

第 58–59 行 `test_close_reduce_failure_leaves_closing_and_is_resumable`，把

```python
    gid = gx.open('fake', SYM, GP)
    assert fake.fetch_positions(SYM).net_size > 0
```

改为

```python
    gid = gx.open('fake', SYM, GP)
    fake.set_price(SYM, 98.5); gx.sync(gid, SYM) # 驱动买线成交 → 累出净多
    assert fake.fetch_positions(SYM).net_size > 0
```

> 注：这三处 `gx` 走 `resilient`/`faulty` 包装，但空 schedule 下透传；`fake.set_price` + `gx.sync` 在设置故障 schedule 之前执行，故障只作用于后续 close 的 reduce 市价单。

- [ ] **Step 10: 修 `test_cycles.py::test_monitor_cycle_resumes_stuck_closing_grid`**

`tests/runtime/test_cycles.py` 第 190–194 行，把

```python
    ex, store, gx, mgr = _setup(store, 100.0)
    gid = mgr.open_proposals([_proposal()])[0]
    g = gx.grids.get(gid)
    gx.grids.transition_status(gid, 'CLOSING', expected_version=g.version)  # 卡住
    assert ex.fetch_positions(BTC).net_size > 0
```

改为

```python
    ex, store, gx, mgr = _setup(store, 100.0)
    gid = mgr.open_proposals([_proposal()])[0]
    ex.set_price(BTC, 98.5); gx.sync(gid, BTC)   # 真中性：驱动买线成交 → 累出净多
    g = gx.grids.get(gid)
    gx.grids.transition_status(gid, 'CLOSING', expected_version=g.version)  # 卡住
    assert ex.fetch_positions(BTC).net_size > 0
```

- [ ] **Step 11: 跑全套，确认全绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: all passed（0 failed）。若 `test_monitor_triggers_fixed_stop_and_closes` 或 `test_run_monitor_cycle_triggers_stop_close` 意外失败，说明净多浮亏未过 −3.4% 阈值——把这两个测试里的 `set_price(..., 96.5)` 改为 `96.0`（更深跌保证触发）后再跑。

- [ ] **Step 12: 提交**

```bash
git add gridtrade/execution/grid_executor.py gridtrade/execution/reconciler.py \
        tests/execution/test_grid_executor.py tests/execution/test_monitor.py \
        tests/execution/test_chaos_close.py tests/runtime/test_cycles.py
git commit -m "feat(grid): 开网即 flat 真中性（去 init 底仓，open+restore 对称）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 回归锁——净空下记账精确（e2e 现金流真值）

**Files:**
- Create: `tests/execution/test_neutral_accounting.py`

**Interfaces:**
- Consumes: Task 1 的中性 `open()`（开网 flat）+ `GridExecutor.sync` + `LiveEquity.snapshot(mark)`。
- Produces: 无（纯回归锁；无生产改动）。

- [ ] **Step 1: 写 e2e 记账精确测试**

Create `tests/execution/test_neutral_accounting.py`：

```python
"""回归锁：真中性网格记账在净多/净空/穿零下均等于模型无关的现金流盯市真值。
真值 = Σ(卖入现金 − 买出现金) + 期末净仓×mark − 真实费。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 10,
      'stop_low_price': 80.0, 'stop_high_price': 120.0}
CAP, LEV = 1000.0, 5.0


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.001, 1e-6, 1e-6, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    return ex, GridExecutor(ex, store, cap=CAP, leverage=LEV)


def _drive(gx, ex, gid, path):
    """细步长逐线成交（贴近真实连续行情，避免批量成交破坏 last_touch 链），每步后 sync。"""
    cur = ex.fetch_price(SYM)
    for target in path:
        step = 0.1 if target >= cur else -0.1
        p = cur
        while (step > 0 and p < target) or (step < 0 and p > target):
            p = round(p + step, 4)
            if (step > 0 and p > target) or (step < 0 and p < target):
                p = target
            ex.set_price(SYM, p)
            gx.sync(gid, SYM)
        cur = target


def _oracle_pnl(ex, mark):
    cash = fees = 0.0
    for t in ex.fetch_my_trades(SYM):
        cash += t.price * t.size if t.side == 'sell' else -t.price * t.size
        fees += t.fee
    return cash + ex.fetch_positions(SYM).net_size * mark - fees


def test_neutral_accounting_exact_ending_long(store):
    ex, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    _drive(gx, ex, gid, [90, 110, 95, 105, 97])
    mark = 103.3
    eng = (gx.live[gid].snapshot(mark)['net_value'] - 1.0) * CAP
    assert abs(eng - _oracle_pnl(ex, mark)) < 1e-6


def test_neutral_accounting_exact_sustained_short(store):
    ex, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    _drive(gx, ex, gid, [110])          # 涨到顶 → 持续净空
    mark = 112.0
    snap = gx.live[gid].snapshot(mark)
    assert snap['net_position'] < 0
    eng = (snap['net_value'] - 1.0) * CAP
    assert abs(eng - _oracle_pnl(ex, mark)) < 1e-6


def test_neutral_accounting_exact_zero_crossing_to_short(store):
    ex, gx = _setup(store)
    gid = gx.open('fake', SYM, GP)
    _drive(gx, ex, gid, [90, 110, 92, 108, 100, 111])   # 多次穿零收净空
    mark = 108.0
    snap = gx.live[gid].snapshot(mark)
    assert snap['net_position'] < 0
    eng = (snap['net_value'] - 1.0) * CAP
    assert abs(eng - _oracle_pnl(ex, mark)) < 1e-6
    assert abs(snap['net_position'] - ex.fetch_positions(SYM).net_size) < 1e-9
```

- [ ] **Step 2: 运行，确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_neutral_accounting.py -q`
Expected: PASS（3 passed）。此测试锁定审计结论——若跑在 Task 1 之前的含-init 代码上会因 init 漂移失败。

- [ ] **Step 3: 提交**

```bash
git add tests/execution/test_neutral_accounting.py
git commit -m "test(grid): 回归锁——净空/穿零下中性记账等于现金流盯市真值

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 回归锁——中性破顶 high 保险丝平空并撑网全拆（e2e）

**Files:**
- Create: `tests/execution/test_neutral_fuse.py`

**Interfaces:**
- Consumes: Task 1 的中性 `open()`（`stop_orders_enabled=True` 时开网即挂两张 reduce-only 触发单）；`Reconciler(executor).reconcile_fuses(grid_id, symbol) -> {'replaced': int, 'fired': bool}`。
- Produces: 无（纯回归锁；无生产改动）。

- [ ] **Step 1: 写保险丝净空 e2e 测试**

Create `tests/execution/test_neutral_fuse.py`：

```python
"""回归锁：真中性网格涨破 stop_high 时为净空，high 保险丝(buy reduce-only)须平掉空头，
对账判定已触发后撑网全拆。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import CLOSED

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 10,
      'stop_low_price': 85.0, 'stop_high_price': 115.0}


def test_neutral_top_breakout_high_fuse_covers_short_and_closes(store):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.001, 1e-6, 1e-6, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=True, stop_slippage=0.15)
    gid = gx.open('fake', SYM, GP)
    # 涨到网格顶：卖线成交 → 净空
    for p in [102, 105, 108, 110]:
        ex.set_price(SYM, p); gx.sync(gid, SYM)
    assert ex.fetch_positions(SYM).net_size < 0
    # 破 stop_high：high 保险丝(buy reduce-only)触发，把空头平向 0
    ex.set_price(SYM, GP['stop_high_price'] + 0.5)
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9
    # 对账判定保险丝已触发 → 撑网全拆
    out = Reconciler(gx).reconcile_fuses(gid, SYM)
    assert out['fired'] is True
    assert gx.grids.get(gid).status == CLOSED
```

- [ ] **Step 2: 运行，确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_neutral_fuse.py -q`
Expected: PASS（1 passed）。

- [ ] **Step 3: 提交**

```bash
git add tests/execution/test_neutral_fuse.py
git commit -m "test(stop): 回归锁——中性破顶 high 保险丝平空 + 撑网全拆

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 回测入口切中性 + 删除废弃 init 单测

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`（约 81–83 行）
- Modify: `tests/execution/test_live_equity.py`（删 `test_neutral_init_base_inventory`，约 76–86 行）

**Interfaces:**
- Consumes: `simulate_grid_engine(..., neutral_init=...)`（参数保留，仅改回测入口传值）。
- Produces: 回测与新实盘同口径（均无 init）。

- [ ] **Step 1: 回测入口传 `neutral_init=False`**

`gridtrade/backtest/backtest_run.py` 第 81–83 行，把

```python
        sim = simulate_grid_engine(bars_df, gp, cap=1000.0, leverage=lev, fee=fee_rate,
```
（该调用块结尾）
```python
                                   funding_df=funding_df, neutral_init=True)
```

改为

```python
                                   funding_df=funding_df, neutral_init=False)
```

- [ ] **Step 2: 删除锁定废弃策略的单测**

`tests/execution/test_live_equity.py` 删除第 76–86 行整个 `test_neutral_init_base_inventory` 函数（它复刻的正是被废弃的 OKX init 底仓；`LiveEquity` 数学本身由 `test_replay_matches_incremental` 等其余测试覆盖）。删除后确保上下测试之间保留一个空行。

- [ ] **Step 3: 运行受影响测试，确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_run.py tests/execution/test_live_equity.py tests/core/test_grid_engine_parity.py -q`
Expected: PASS —— `test_run_backtest_end_to_end` 只做结构性断言（非空/pnl 非 NaN/exit_reason 是串），不 hardcode 数值，故切中性后仍绿；金标 parity 用默认 `neutral_init=True` 不受影响。

- [ ] **Step 4: 提交**

```bash
git add gridtrade/backtest/backtest_run.py tests/execution/test_live_equity.py
git commit -m "feat(backtest): 回测入口切中性(neutral_init=False)，与新实盘同口径；删废弃 init 单测

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 文档 + 全套绿验证

**Files:**
- Modify: `docs/STATUS.md`（§7 开网描述）

**Interfaces:** 无。

- [ ] **Step 1: 更新 STATUS.md 开网描述**

`docs/STATUS.md` §7（约 146 行），把

```
- ✅ **开网格**（选币→中性市价底仓→26 限价挂单→ACTIVE）
```

改为

```
- ✅ **开网格**（选币→**真中性、开网即 flat 无底仓**→26 限价挂单→ACTIVE；价涨转净空/价跌转净多）
```

- [ ] **Step 2: 跑全套，确认全绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: all passed, 0 failed（相对改造前：删 1 测试、加 5 测试 = 净 +4）。

- [ ] **Step 3: 提交**

```bash
git add docs/STATUS.md
git commit -m "docs(status): 网格开仓改为真中性（开网即 flat 无底仓）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 4（非代码，交接给人工上线）：清场切换 runbook**

部署前先把旧（含 init 多头）活跃网格平到 flat，再部署中性版：
1. 控制台 `PANIC_CLOSE_ALL`（web 控制台或 control_commands）平掉所有活跃网格。
2. 确认 `fly logs` 无残仓、`grids` 无 ACTIVE。
3. 推分支 / 触发 CD 部署中性版本。
4. 下个 scheduler 整点开中性网格，核验：新网 `fetch_my_trades` 无 `:init:` 成交、开网 `net≈0`；随价上行 `grid_accounting.net_position` 转负（净空）。
5. testnet 有机验证：观测一次自然破顶，确认 high 保险丝触发平空（HL `reduce_only` 封顶 + 触发参考价属 STATUS.md §5 待验证项，此处吃重）。

---

## Self-Review

**1. Spec coverage（逐条对 spec §4/§5/§6/§7）：**
- §4.2 改动点 open/restore/backtest → Task 1（open+restore）、Task 4（backtest）。✅
- §4.3 保持不变清单 → 无改动，Task 5 全绿验证守护。✅
- §5.1 改现有测试（grid_executor/live_equity/monitor/chaos_close/idempotent/cycles）→ Task 1 Step 7–10 + Task 4 Step 2。注：`test_grid_executor_idempotent.py:40` 用捕获值 `net_after_first`（非硬编码），中性下自动成立，无需改（已在 Task 1 Step 11 全套验证覆盖）。✅
- §5.2 新增测试（flat/符号跟随/记账精确/restore 对称/保险丝）→ Task 1（flat+符号）、Task 2（记账精确，含 restore 一致的净仓==交易所断言）、Task 3（保险丝）。restore 净空往返的对称性由既有 `test_restore_rebuilds_state_matching_pre_restart`（before/after 相等）+ Task 2 的 `net_position==exchange` 断言共同守护。✅
- §6 迁移 runbook → Task 5 Step 4。✅
- §7 风险（HL reduce_only/触发参考价）→ Task 5 Step 4 第 5 点标注。✅

**2. Placeholder scan：** 无 TBD/TODO/「类似上文」；每个改动都给了 before/after 完整代码与确切命令。✅

**3. Type consistency：** `open()`/`restore()`/`reconcile_fuses()`/`snapshot()` 签名与返回键（`net_value`/`net_position`/`fired`）均与现有代码一致；测试用 `_setup`/`GP`/`SYM` 沿用各文件既有约定。✅

**已知非硬失败点（已在计划内兜底）：** 止损阈值型测试（monitor/cycles 的 `96.5`）在中性下由「买线成交累出净多浮亏」触发，估算 pnl ≈ −4.7%（5 条买线 × 每格量），过 −3.4% 阈值有余；若环境数值微差未触发，Task 1 Step 11 给了降到 `96.0` 的兜底。
