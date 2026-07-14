# 部分成交残额补单收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 网格补对侧单的守卫从"对侧线有 open 单就跳过"精确化为"对侧线有 **filled==0 的满额单** 才跳过"——对侧是残额单（部分成交后 filled>0）时照挂整额 order_num 回购单，与残额单同线并存，消除净仓永久偏差 1×order_num。

**Architecture:** 两处守卫（`sync` 内联块 + `_replenish_opposite`）的判据从存在性集合 `open_lines` 改为满额占位集合 `full_lines`（只含 filled==0 的 open 单）。对账/重启/关格零改动——本仓对账按 `exchange_order_id` 而非 line 工作（spec §三实测坐实）。前置：给 FakeExchange 加部分成交测试钩子（现 `_match` 原子全额，此边界跑不出来）。

**Tech Stack:** Python 3.9、pytest。无新依赖，不碰 core/backtest（回测几何逐位不变）。

**Spec:** `docs/superpowers/specs/2026-07-15-partial-fill-replenish-guard-design.md`（已批准；本计划引用其章节号）。

## Global Constraints

- Python 3.9；中文注释 + spec 引用（仓库风格）。
- **判据语义（spec §3.1）**：跳过条件 = 对侧 `(line,side)` **存在任一 `filled == 0` 的 open 单**；否则（无 open 单 **或** 全是残额 filled>0）→ 照挂整额 `order_num` 回购单。
- **双倍建仓防护是红线（spec §3.3）**：filled==0 满额单占位时**绝不**重复挂单（testnet OP/gt00 事故）。精确化不得削弱它——必须有回归钉死。
- **不碰 core/backtest**：`grid_engine`/回测几何逐位不变，golden/core 测试必须绿（spec §五）。
- **FakeExchange 部分成交仅测试路径**（spec §3.2）：不改回测引擎几何。
- **实测不变量（spec §四）**：偏差恒 1.00×order_num；账本 net==交易所净仓；对账按 order_id；同线两单各带独立 exchange_order_id。
- 测试命令：`.venv/bin/python -m pytest <path> -q -o addopts=""`（`-o addopts=""` 是本仓惯例，避免 `-q` 摘要行被吞）。
- 每个任务末尾 commit，消息风格 `feat(scope): 中文摘要(spec 2026-07-15 §N)`，末尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- **不部署**（部署由主运维会话按"避开整点 HH:00–HH:12 选币窗口"手动做）。

## 关键代码事实（实测，编码时直接引用勿再查）

- 两处守卫是**全项目仅有**的按 `(line,side)` 索引挂单的代码：`grid_executor.py:171-173`（`_replenish_opposite`）与 `grid_executor.py:211`+`:251`（`sync` 内联块）。选币/关格/面板都按 fill 的 line 聚合，与挂单簿无关。
- `grid_orders` 主键**只有 `client_oid`**，无 `(grid_id,line,side)` 唯一约束；`_next_oid` 带 seq → DB 天然支持同线多单。
- `Reconciler.restore` 只重建 `geom`（price_array/order_num），**不重建 orders 表**（挂单持久化在 DB）；`reconcile_open_orders` 的 `expected`/`protected`/`missing` 全是 `{exchange_order_id: order}` 字典 → 同线两单各自对账。
- 探针实证：补上被跳过的整额回购单 → 终态净仓精确还原 `+1.00×order_num`、与正常路径逐位一致、账本零漂移；同线两单经完整重启对账周期 → 2 单存活、无误撤/漏挂/重复。
- `FakeExchange._fill(o, fill_price)`（fake.py:65-80）用 `o.size` 作成交量；`_match`（:55-63）原子全额撮合并 `del self._open[oid]`。
- 测试用 `store` fixture（`def test_x(store):`），`GridExecutor(ex, store, cap=..., gearing=...)`，仿 `tests/execution/test_close_share.py`。

---

### Task 1: FakeExchange 部分成交测试钩子（spec §3.2）

**Files:**
- Modify: `gridtrade/exchanges/fake.py`（`_fill` 加 `qty` 参数 + 新增 `partial_fill` 钩子）
- Test: `tests/exchanges/test_fake.py`

**Interfaces:**
- Produces（Task 2 消费）：`FakeExchange.partial_fill(symbol, price, qty) -> bool`——让 `price` 处的挂单只成交 `qty`、残量留簿（size 减 qty、仍 open、filled=0）；成交 `Trade.order_id` = 原单 id（执行器按 order_id 映射回线）；命中返回 True。`_fill(o, fill_price, qty=None)`——qty=None 时全额（原行为），否则只成交 qty。
- Consumes: 无。

- [ ] **Step 1: 写失败测试**

在 `tests/exchanges/test_fake.py` 末尾追加：

```python
def test_partial_fill_leaves_remnant_and_records_trade():
    # 部分成交测试钩子（spec 2026-07-15 §3.2）：触价只成交 qty，残量留簿 filled=0，
    # 成交 Trade.order_id=原单 id（执行器按 order_id 映射回网格线）
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange
    BTC = 'BTC/USDT:USDT'
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 1e-6, 1e-6, 'live', 0)], price=100.0)
    o = ex.create_limit_order(BTC, 'buy', 99.0, 10.0, client_oid='g:1')   # 不立即成交（现价100>99）
    hit = ex.partial_fill(BTC, 99.0, 3.0)
    assert hit is True
    # 残单留簿：同 id、剩 7、filled=0、仍 open
    rem = [x for x in ex.fetch_open_orders(BTC) if x.id == o.id]
    assert len(rem) == 1 and abs(rem[0].size - 7.0) < 1e-9 and rem[0].filled == 0.0
    # 成交流水：一笔 size=3、order_id=原单 id、方向 buy
    tr = [t for t in ex.fetch_my_trades(BTC) if t.order_id == o.id]
    assert len(tr) == 1 and abs(tr[0].size - 3.0) < 1e-9 and tr[0].side == 'buy'
    # 净仓 = 已成交部分
    assert abs(ex.fetch_positions(BTC).net_size - 3.0) < 1e-9


def test_partial_fill_miss_returns_false():
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange
    BTC = 'BTC/USDT:USDT'
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 1e-6, 1e-6, 'live', 0)], price=100.0)
    ex.create_limit_order(BTC, 'buy', 99.0, 10.0, client_oid='g:1')
    assert ex.partial_fill(BTC, 88.0, 3.0) is False       # 无该价位挂单
    assert ex.partial_fill(BTC, 99.0, 10.0) is False      # qty>=size 不算部分成交


def test_partial_then_full_via_setprice_closes_order():
    # 残单被 set_price 触及 → 剩余量全额成交，同 order_id 第二笔成交（执行器据此判吃满）
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange
    BTC = 'BTC/USDT:USDT'
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 1e-6, 1e-6, 'live', 0)], price=100.0)
    o = ex.create_limit_order(BTC, 'buy', 99.0, 10.0, client_oid='g:1')
    ex.partial_fill(BTC, 99.0, 3.0)
    ex.set_price(BTC, 99.0)                                # 价格落到 99 → 残 7 全额成交
    tr = sorted((t.size for t in ex.fetch_my_trades(BTC) if t.order_id == o.id))
    assert tr == [3.0, 7.0]                                # 两笔累计 = 原单 10
    assert not [x for x in ex.fetch_open_orders(BTC) if x.id == o.id]   # 已离簿
    assert abs(ex.fetch_positions(BTC).net_size - 10.0) < 1e-9
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_fake.py -q -o addopts=""`
预期：3 个新测试 FAIL（`FakeExchange` 无 `partial_fill` 属性）。

- [ ] **Step 3: 实现**

`gridtrade/exchanges/fake.py` 的 `_fill` 加 `qty` 参数（fill_price 后）。把方法体里的成交量从 `o.size` 改为 `q`：

```python
    def _fill(self, o: Order, fill_price: float, qty: float = None) -> None:
        q = o.size if qty is None else float(qty)   # qty!=None：部分成交，只成交 q（partial_fill 钩子用）
        signed = q if o.side == 'buy' else -q
        pos = self._pos.get(o.symbol, Position(o.symbol, 0.0, 0.0))
        new_net = pos.net_size + signed
        # 同向加仓更新加权均价；反向或反手时简单处理（净仓符号不翻转的减仓保留均价）
        if pos.net_size == 0 or (pos.net_size > 0) == (signed > 0):
            denom = abs(new_net) if new_net != 0 else 1.0
            avg = (abs(pos.net_size) * pos.avg_price + abs(signed) * fill_price) / denom
        else:
            avg = pos.avg_price if (pos.net_size > 0) == (new_net >= 0) else fill_price
        self._pos[o.symbol] = Position(o.symbol, new_net, avg)
        tid = next(self._ts)
        self._trades.append(Trade(
            id=str(tid), client_oid=o.client_oid, symbol=o.symbol,
            side=o.side, price=fill_price, size=q,
            fee=q * fill_price * self._fee_rate, ts=tid, order_id=o.id))
```

在测试钩子区（`seed_quote_volumes` 之后，`_price_of` 之前）新增：

```python
    def partial_fill(self, symbol: str, price: float, qty: float) -> bool:
        """测试钩子（spec 2026-07-15 §3.2）：让 price 处的挂单只成交 qty、残量留簿，
        模拟"触价部分成交后反弹"。成交 Trade.order_id=原单 id（执行器按 order_id 映射回网格线）；
        残单保持同 id、size 减 qty、filled=0、仍 open。不联动 _match（不触发整簿撮合）。
        命中（存在该价位挂单且 0<qty<size）返回 True，否则 False。"""
        for oid, o in list(self._open.items()):
            if o.symbol == symbol and abs(o.price - price) < 1e-12 and 0 < qty < o.size:
                self._fill(o, o.price, qty=qty)      # 记 Trade(qty) + 更新净仓
                self._open[oid] = Order(id=o.id, client_oid=o.client_oid, symbol=symbol,
                                        side=o.side, price=o.price, size=o.size - qty,
                                        filled=0.0, status='open', reduce_only=o.reduce_only)
                return True
        return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_fake.py -q -o addopts=""`
预期：全 PASS（含既有 `test_fake.py` 用例——`_fill` 加默认参数不破坏全额路径）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/fake.py tests/exchanges/test_fake.py
git commit -m "feat(exchanges): FakeExchange 部分成交测试钩子——partial_fill 触价只成交部分/残量留簿(残额补单收口的测试前置,spec 2026-07-15 §3.2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 守卫按 filled 精确化（两处）+ 核心收口/双倍建仓/重启对账测试（spec §3.1、§3.3、§四）

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`（`_replenish_opposite` guard + `sync` 内联块 guard）
- Test: `tests/execution/test_partial_fill_replenish.py`（新建）

**Interfaces:**
- Consumes: Task 1 `FakeExchange.partial_fill`。
- Produces: 无新公共接口（守卫行为精确化）。

**判据变更（两处同一语义）**：`{(line,side)}` 存在性集合 → `full_lines = {(line,side) : ∃ open 单 filled==0}` 满额占位集合。跳过仅当对侧有 filled==0 满额单；残额单不占位 → 照挂整额回购单。

- [ ] **Step 1: 写失败测试（新建 `tests/execution/test_partial_fill_replenish.py`）**

```python
"""部分成交残额补单收口（spec 2026-07-15）：守卫从"对侧有 open 单就跳过"精确化为
"对侧有 filled==0 满额单才跳过"——残额单照挂整额回购单，消除净仓永久偏差 1×order_num。
双倍建仓防护（filled==0 满额单占位不重复挂）是红线，一并钉死。"""
from gridtrade.exchanges.base import Instrument, Order
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.models import GridOrder

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 96.0, 'high_price': 104.0, 'grid_count': 8,
      'stop_low_price': 95.0, 'stop_high_price': 105.0}


def _open_grid(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 1e-6, 1e-6, 'live', 0)], price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, gearing=3.4)
    gid = gx.open('fake', BTC, dict(GP), tag='t')
    return ex, gx, gid


def _line_qty(gx, gid, li, side):
    return sum(float(o.size) - float(o.filled or 0) for o in gx.orders.list_by_grid(gid)
               if o.status == 'open' and o.line_index == li and o.side == side)


def test_remnant_line_gets_full_replenish_restoring_model_position(store):
    # 核心（spec §四实测复刻）：L4 买单部分成交(残额)后卖单成交 → 守卫照挂整额回购单，
    # 终态净仓精确还原 +1.00×order_num（= 正常全额路径），账本零漂移。
    ex, gx, gid = _open_grid(store)
    q = gx._geom[gid]['order_num']; pa = gx._geom[gid]['price_array']
    pk, pk1 = pa[4], pa[5]                         # L4 最高买线、L5 最低卖线
    ex.partial_fill(BTC, pk, q * 0.30); gx.sync(gid, BTC)   # L4 只吃 30%，残额留簿
    ex.set_price(BTC, pk1); gx.sync(gid, BTC)      # 升到 L5 → 卖单成交，触发补 L4 回购单
    # 修复前：守卫见 (L4,buy) 已 open 就跳过 → L4 只有残额 0.7q；修复后：加挂整额回购单
    assert _line_qty(gx, gid, 4, 'buy') > q * 1.5  # 残额 0.7q + 回购 1.0q ≈ 1.7q
    ex.set_price(BTC, pk); gx.sync(gid, BTC)        # 回落 L4 → 两张都成交
    pos = ex.fetch_positions(BTC).net_size
    snap = gx.live[gid].snapshot(pk)
    assert abs(pos - q) < 1e-6                      # 净仓 = +1.00×order_num（还原模型）
    assert abs(snap['net_position'] - pos) < 1e-9  # 账本 == 交易所（零漂移）


def test_full_order_still_blocks_double_build(store):
    # 红线（spec §3.3）：对侧线有 filled==0 满额单时，重复 sync 不得产生第二张单
    # （testnet OP/gt00 双倍建仓事故防护——精确化绝不能削弱）。
    ex, gx, gid = _open_grid(store)
    pa = gx._geom[gid]['price_array']
    ex.set_price(BTC, pa[4]); gx.sync(gid, BTC)     # L4 买单全额成交 → 补 L5 卖 & L3 买（满额）
    n_before = len([o for o in gx.orders.list_by_grid(gid) if o.status == 'open'])
    gx.sync(gid, BTC); gx.sync(gid, BTC)            # 重复 sync：满额单占位，不得重复挂
    n_after = len([o for o in gx.orders.list_by_grid(gid) if o.status == 'open'])
    assert n_after == n_before                      # 挂单数不增（无双倍建仓）
    # 每条 (line,side) 至多一张 open 单
    from collections import Counter
    c = Counter((o.line_index, o.side) for o in gx.orders.list_by_grid(gid) if o.status == 'open')
    assert max(c.values()) == 1


def test_replenish_opposite_path_same_guard(store):
    # _replenish_opposite（E2 兜底路径）同款精确化：残额线不挡整额回购单
    ex, gx, gid = _open_grid(store)
    q = gx._geom[gid]['order_num']
    # 把 L4 买单改成残额态（filled>0、open）
    for o in gx.orders.list_by_grid(gid):
        if o.line_index == 4 and o.side == 'buy' and o.status == 'open':
            gx.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=gid, line_index=4,
                                       side='buy', price=o.price, size=o.size, status='open',
                                       exchange_order_id=o.exchange_order_id, filled=q * 0.3))
    # 直接调 _replenish_opposite 补 L4（模拟 L5 卖单吃满的兜底路径）→ 残额不挡，应补
    assert gx._replenish_opposite(gid, BTC, 5, 'sell') is True
    assert _line_qty(gx, gid, 4, 'buy') > q * 1.5   # 残额 + 整额回购单并存


def test_two_orders_same_line_survive_restart_reconcile(store):
    # 不变量①（spec §四实测）：同线两单（残额+整额回购）经 restore+reconcile → 各带独立
    # exchange_order_id、逐单对账，2 单存活、无误撤/漏挂/重复。
    from gridtrade.execution.reconciler import Reconciler
    ex, gx, gid = _open_grid(store)
    q = gx._geom[gid]['order_num']; pa = gx._geom[gid]['price_array']
    # 造同线两单：L4 残额单(filled=0.3q) + L4 整额回购单
    for o in gx.orders.list_by_grid(gid):
        if o.line_index == 4 and o.side == 'buy' and o.status == 'open':
            gx.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=gid, line_index=4,
                                       side='buy', price=o.price, size=o.size, status='open',
                                       exchange_order_id=o.exchange_order_id, filled=q * 0.3))
    oid2 = gx._next_oid(gid, 4)
    o2 = ex.create_limit_order(BTC, 'buy', pa[4], q, post_only=False, client_oid=oid2)
    gx.orders.upsert(GridOrder(client_oid=oid2, grid_id=gid, line_index=4, side='buy',
                               price=pa[4], size=q, status='open', exchange_order_id=o2.id))
    before = sorted((o.line_index, o.side, o.exchange_order_id) for o in gx.orders.list_open_by_grid(gid))
    # 模拟重启：新 executor + restore + reconcile
    gx2 = GridExecutor(ex, store, cap=1000.0, gearing=3.4)
    rec = Reconciler(gx2); rec.restore(gid); rec.reconcile_open_orders(gid, BTC)
    after = sorted((o.line_index, o.side, o.exchange_order_id) for o in gx2.orders.list_open_by_grid(gid))
    n_l4 = sum(1 for o in gx2.orders.list_open_by_grid(gid) if o.line_index == 4 and o.side == 'buy')
    on_exch = sum(1 for o in ex.fetch_open_orders(BTC) if abs(o.price - pa[4]) < 1e-9)
    assert before == after and n_l4 == 2 and on_exch == 2   # 逐位一致、两单存活、零误撤漏挂
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_partial_fill_replenish.py -q -o addopts=""`
预期：`test_remnant_line_gets_full_replenish...` 与 `test_replenish_opposite_path_same_guard` FAIL（残额单被误当满额单，回购单未挂 → L4 挂量只有 0.7q，且终态净仓=0 而非 q）。`test_full_order_still_blocks_double_build` 与 `test_two_orders_same_line_survive_restart_reconcile` 可能已 PASS（守卫旧行为对满额路径正确、对账本就按 order_id）——这两条是防回归的护栏。

- [ ] **Step 3: 实现（两处守卫精确化）**

**① `_replenish_opposite`（grid_executor.py:171-174）**——把：

```python
        open_lines = {(o.line_index, o.side)
                      for o in self.orders.list_by_grid(grid_id) if o.status == 'open'}
        if (opp_line, opp_side) in open_lines:
            return False
```

改为：

```python
        # 满额占位集合（spec 2026-07-15 §3.1）：只有对侧线存在 filled==0 的满额 open 单才跳过；
        # 残额单(filled>0)不占位 → 照挂整额回购单（双倍建仓防护不变——满额单仍挡）。
        full_lines = {(o.line_index, o.side)
                      for o in self.orders.list_by_grid(grid_id)
                      if o.status == 'open' and float(o.filled or 0.0) == 0.0}
        if (opp_line, opp_side) in full_lines:
            return False
```

**② `sync` 内联块**——三处改动：

(a) grid_executor.py:209-211 的注释与集合构造，把：

```python
        # 已 resting 的 (line,side) 集合：补对侧单前查重，防同 line 同向重复挂单
        # → 双倍建仓（testnet OP/gt00 实证：中性网格价格震荡下重复单持久叠加）。
        open_lines = {(o.line_index, o.side) for o in _all if o.status == 'open'}
```

改为：

```python
        # 满额占位集合（spec 2026-07-15 §3.1）：只有 filled==0 的满额 open 单才占位、才挡补单
        # → 双倍建仓防护不变（testnet OP/gt00 实证）；残额单(filled>0)不占位，照挂整额回购单
        # （修部分成交残额窗口：残单误当满额单 → 回购单永不挂出、净仓永久差 1×order_num）。
        full_lines = {(o.line_index, o.side) for o in _all
                      if o.status == 'open' and float(o.filled or 0.0) == 0.0}
```

(b) grid_executor.py:241-244——把 `by_oid[t.order_id] = go` 之后、`if not fully:` 之前插入一行 discard，并**删除**原第 244 行（`fully` 路径里的 discard）。即把：

```python
            by_oid[t.order_id] = go        # 同轮多笔部分成交累计正确
            if not fully:
                continue                   # 未吃满:线仍占用、不补单,等后续部分成交
            open_lines.discard((line_index, t.side))   # 吃满离场，其 (line,side) 腾空
```

改为：

```python
            by_oid[t.order_id] = go        # 同轮多笔部分成交累计正确
            # 该线一旦有成交(部分/全额)即非 filled==0 满额单 → 腾出满额占位（spec 2026-07-15）：
            # 部分成交后残额单不再占位,下方兄弟吃满时照挂整额回购单;全额则本就离场。
            full_lines.discard((line_index, t.side))
            if not fully:
                continue                   # 未吃满:线仍占用、不补单,等后续部分成交
```

（注意：原 244 行的 discard 已并入上面，删除即可，不要重复。）

(c) grid_executor.py:251 的补单查重守卫与其后的 add，把：

```python
                    # opp_line 已有同向 resting 单则不重复挂（防双倍建仓）
                    if (opp_line, opp_side) not in open_lines:
```

改为：

```python
                    # opp_line 已有 filled==0 满额单则不重复挂（防双倍建仓，spec 2026-07-15 §3.3）
                    if (opp_line, opp_side) not in full_lines:
```

并把该块末尾的 `open_lines.add((opp_line, opp_side))` 改为 `full_lines.add((opp_line, opp_side))`（新挂的是整额回购单，filled=0，进满额占位集）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/execution/test_partial_fill_replenish.py tests/execution/ -q -o addopts=""`
预期：新文件 4 用例全 PASS；`tests/execution/` 全绿（既有 `test_partial_fills.py`/`test_sync_replenish_dup.py` 等不受影响——满额路径行为不变）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/execution/grid_executor.py tests/execution/test_partial_fill_replenish.py
git commit -m "feat(execution): 补单守卫按 filled 精确化——残额单不占位、照挂整额回购单,消除净仓永久偏差 1×order_num;双倍建仓防护(filled==0 满额单占位)不变(spec 2026-07-15 §3.1§3.3)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 全量验证 + 回测几何不变证明

**Files:** 无代码改动（验证收尾）。

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/python -m pytest -q -o addopts=""`
预期：全 PASS（基线 848 passed / 2 skipped + 本计划新增用例；skip 为 Postgres 门控，正常）。

- [ ] **Step 2: golden + core parity（回测几何未动的证明，spec §五）**

Run: `.venv/bin/python -m pytest tests/golden/ tests/core/ -q -o addopts=""`
预期：全 PASS——本收口**未改** `core/`/`backtest/`（`grid_engine` 只被读、未被改），回测几何逐位不变。

- [ ] **Step 3: 守卫唯一性自查（spec §三：全项目仅两处按 (line,side) 索引挂单）**

```bash
grep -rn "line_index, o.side\|line_index, t.side\|(line,side)\|open_lines\|full_lines" gridtrade/execution/grid_executor.py
```
预期：只有 `_replenish_opposite` 与 `sync` 内联块两处出现 `full_lines`（已精确化）；**不应再有 `open_lines`**（两处都已改名）。若 grep 到残留 `open_lines`，说明有一处漏改。

- [ ] **Step 4: Commit（若扫尾有改动）**

```bash
git add -A
git commit -m "chore: 残额补单收口收尾——全量+golden 绿,守卫唯一性自查(spec 2026-07-15 §五)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 未尽事项（明确不在本计划内）

- **不部署**：部署由主运维会话手动做（避开整点 HH:00–HH:12 选币窗口）。
- **回测建模部分成交**：独立议题（历史逐笔无归档），spec §六非目标。
- **残额单"补到满"（理解 A）**：已否决（spec §六）——模型要残额+回购两笔独立意图，补到 order_num 反把恒定偏差变随机值、且撤挂有竞态/超配风险。
