# 交易所解耦重构 P4x 实现计划（执行层 fill/对账 改按 exchange order id 匹配）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** 修复 HL 端到端最后卡点：HL 下单要求 cloid 为 128-bit hex（我们的 `grid:line:seq` 字符串 → 422），且 **HL 的 fill 与 open order 都只带 `oid`(order id)、不带 cloid**（见 ccxt parse_trade/parse_order）。把执行层的「fill→网格线」与「挂单对账」从 **client_oid 字符串匹配**改成 **exchange order id 匹配**（统一、跨所通用）；HL 下单**省略 cloid**（可选字段，避免非法 hex）。client_oid 仍作我方存储标识保留；成交幂等仍靠 trade_id（不变）。

**Architecture:** 新增 `Trade.order_id`（成交所属交易所订单号），各适配器填充（ccxt 取 `r['order']`，Fake 取 `o.id`）。`grid_executor.sync` 用 `{grid_orders.exchange_order_id → grid_order}` 把 fill 映射到 line（替代 `client_oid.split(':')`）。`reconciler.reconcile_open_orders` 按 order id 比对（替代 client_oid）。新增 `ExchangeAdapter.encode_cloid`（默认原样；HL 返回 None=省略），`CcxtAdapter._params` 据它决定是否发 `clientOrderId`。FakeExchange 行为等价 → 现有金标测试回归即主证。

**Tech Stack:** Python 3.9、pytest、FakeExchange + 内存 SQLite。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 这是金标测试过的执行核心，任何不确定停下来问。

## Global Constraints

- Python 3.9；改 `gridtrade/exchanges/{base,ccxt_adapter,hyperliquid,fake}.py`、`gridtrade/execution/{grid_executor,reconciler}.py` 及测试。不改 core/state/backtest/其它。
- **现有全套测试必须保持绿**（金标回归）：`TZ=Asia/Shanghai .venv/bin/python -m pytest`，基线 244 passed。
- client_oid 仍用于：下单传参（OKX 幂等）、grid_orders 存储标识。**匹配（读回交易所）改 order id**。
- HL `encode_cloid` 返回 None（省略 cloid）；OKX/Fake 返回原 client_oid。
- 成交幂等仍靠 `grid_fills.trade_id`（不变）。

---

### Task 1: Trade.order_id + 各适配器填充

**Files:** `gridtrade/exchanges/base.py`、`gridtrade/exchanges/ccxt_adapter.py`、`gridtrade/exchanges/fake.py`、`tests/exchanges/test_fake_order_id.py`

- [ ] **Step 1: 写失败测试** Create `tests/exchanges/test_fake_order_id.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument

SYM = 'BTC/USDT:USDT'


def _ex():
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(SYM, 100.0)
    return ex


def test_fill_trade_carries_order_id():
    ex = _ex()
    o = ex.create_limit_order(SYM, 'buy', 100.0, 1.0, client_oid='g:1:0')  # 立即成交
    trades = ex.fetch_my_trades(SYM)
    assert len(trades) == 1
    assert trades[0].order_id == o.id          # 成交带所属订单号
    assert trades[0].client_oid == 'g:1:0'     # client_oid 仍在


def test_market_order_fill_carries_order_id():
    ex = _ex()
    o = ex.create_market_order(SYM, 'buy', 2.0, client_oid='g:init:0')
    trades = ex.fetch_my_trades(SYM)
    assert trades[-1].order_id == o.id
```

- [ ] **Step 2: 跑测试确认红** `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_fake_order_id.py -q` → FAIL（Trade 无 order_id / 为 None）。

- [ ] **Step 3: 实现**
  - `base.py` 的 `Trade` dataclass 末尾加：`order_id: Optional[str] = None`（`Optional` 已 import）。
  - `fake.py` `_fill` 的 `Trade(...)` 加 `order_id=o.id`。
  - `ccxt_adapter.py` `fetch_my_trades` 的 `Trade(...)` 加 `order_id=(str(r['order']) if r.get('order') is not None else None)`。

- [ ] **Step 4: 跑测试确认绿 + 回归** 上述文件 PASS；`TZ=Asia/Shanghai .venv/bin/python -m pytest` 全绿。

- [ ] **Step 5: 提交** `git add -A && git commit -m "feat(exchanges): Trade.order_id carried on fills (P4x)"`

---

### Task 2: encode_cloid（HL 省略 cloid）+ _params 据它发单

**Files:** `gridtrade/exchanges/base.py`、`gridtrade/exchanges/ccxt_adapter.py`、`gridtrade/exchanges/hyperliquid.py`、`tests/exchanges/test_encode_cloid.py`

- [ ] **Step 1: 写失败测试** Create `tests/exchanges/test_encode_cloid.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter


class _Client:
    def __init__(self): self.calls = []
    def fetch_ticker(self, s): return {'last': 100.0}
    def create_order(self, sym, typ, side, size, price, params):
        self.calls.append(params)
        return {'id': '1', 'symbol': sym, 'side': side, 'price': price or 0.0,
                'amount': size, 'filled': 0.0, 'status': 'open', 'info': {}}


def test_default_encode_cloid_is_identity():
    assert FakeExchange().encode_cloid('g:1:0') == 'g:1:0'


def test_hl_encode_cloid_returns_none():
    assert HyperliquidAdapter(_Client()).encode_cloid('g:1:0') is None


def test_hl_create_order_omits_client_order_id():
    c = _Client()
    HyperliquidAdapter(c).create_limit_order('BTC/USDT:USDT', 'buy', 100.0, 1.0,
                                             client_oid='g:1:0')
    assert 'clientOrderId' not in c.calls[0]    # HL 不发非法 cloid
```

- [ ] **Step 2: 跑测试确认红** `... pytest tests/exchanges/test_encode_cloid.py -q` → FAIL（无 encode_cloid / HL 仍发 clientOrderId）。

- [ ] **Step 3: 实现**
  - `base.py` `ExchangeAdapter` 加默认方法：
    ```python
    def encode_cloid(self, client_oid):
        return client_oid
    ```
  - `ccxt_adapter.py` `_params` 改：
    ```python
    def _params(self, reduce_only, client_oid, post_only=False):
        p = {}
        coid = self.encode_cloid(client_oid) if client_oid else None
        if coid:
            p['clientOrderId'] = coid
        if reduce_only:
            p['reduceOnly'] = True
        if post_only:
            p['postOnly'] = True
        return p
    ```
  - `hyperliquid.py` `HyperliquidAdapter` 加：
    ```python
    def encode_cloid(self, client_oid):
        return None   # HL cloid 须 128-bit hex；省略，改按 order id 匹配
    ```

- [ ] **Step 4: 跑测试确认绿 + 回归** 文件 PASS；全量 PASS。

- [ ] **Step 5: 提交** `git commit -m "feat(exchanges): encode_cloid; HL omits cloid (P4x)"`

---

### Task 3: grid_executor.sync 按 order id 映射 fill→line

**Files:** `gridtrade/execution/grid_executor.py`、`tests/execution/test_sync_orderid.py`

- [ ] **Step 1: 写失败测试** Create `tests/execution/test_sync_orderid.py`（证明即便 fill 的 client_oid 不是网格格式，只要 order_id 对得上仍能映射）:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument, Trade
from gridtrade.state.store import StateStore
from gridtrade.execution.grid_executor import GridExecutor

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    s = StateStore.in_memory(); s.create_all()
    return ex, GridExecutor(ex, s, cap=1000.0, leverage=5.0)


def test_sync_maps_fill_by_order_id_even_with_opaque_client_oid():
    ex, gx = _setup(100.0)
    gid = gx.open('fake', SYM, GP)
    # 取一个已挂的卖单（line 上方），手动注入一笔「client_oid 不可解析、但 order_id 正确」的成交
    open_orders = ex.fetch_open_orders(SYM)
    target = [o for o in open_orders if o.side == 'sell'][0]
    ex._open.pop(target.id, None)                   # 模拟成交：从挂单移除
    ex._trades.append(Trade(id='9001', client_oid='0xdeadbeef-not-grid',
                            symbol=SYM, side='sell', price=target.price,
                            size=target.size, fee=0.0, ts=10_000_000,
                            order_id=target.id))     # 只有 order_id 对得上
    res = gx.sync(gid, SYM)
    assert res['new_fills'] == 1                     # 按 order id 摄入了该成交
```

- [ ] **Step 2: 跑测试确认红** `... pytest tests/execution/test_sync_orderid.py -q` → FAIL（旧码靠 client_oid.startswith/​split，opaque client_oid 不被摄入）。

- [ ] **Step 3: 实现** 把 `grid_executor.py` sync 的第 101-118 行（候选筛选 + line 提取 + 成交单 upsert）改为按 order id：

```python
        trades = self.adapter.fetch_my_trades(symbol, since_ms=cursor)
        by_oid = {o.exchange_order_id: o
                  for o in self.orders.list_by_grid(grid_id) if o.exchange_order_id}
        candidates = [t for t in trades if t.order_id in by_oid]
        candidates.sort(key=lambda t: t.ts)

        new_count = 0
        for t in candidates:
            go = by_oid[t.order_id]
            line_index = go.line_index
            fill = Fill(trade_id=str(t.id), grid_id=grid_id, line_index=line_index,
                        side=t.side, price=float(t.price), size=float(t.size), ts=int(t.ts))
            if not self.fills.add_if_new(fill):
                continue
            new_count += 1
            self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts)
            self.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                                         line_index=line_index, side=t.side, price=t.price,
                                         size=t.size, status='closed'))
```

（补对侧单一段不变；保留其 `exchange_order_id=getattr(order,'id',None)` 存储。）

- [ ] **Step 4: 跑测试确认绿 + 回归** 新测试 PASS；`TZ=Asia/Shanghai .venv/bin/python -m pytest` 全绿（含原 idempotent/monitor/reconciler 金标）。

- [ ] **Step 5: 提交** `git commit -m "feat(execution): sync maps fills by exchange order id (P4x)"`

---

### Task 4: reconciler.reconcile_open_orders 按 order id 对账

**Files:** `gridtrade/execution/reconciler.py`、`tests/execution/test_reconcile_orderid.py`

- [ ] **Step 1: 写失败测试** Create `tests/execution/test_reconcile_orderid.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    s = StateStore.in_memory(); s.create_all()
    return ex, GridExecutor(ex, s, cap=1000.0, leverage=5.0)


def test_reconcile_no_op_when_consistent():
    ex, gx = _setup(100.0)
    gid = gx.open('fake', SYM, GP)
    r = Reconciler(gx).reconcile_open_orders(gid, SYM)
    assert r == {'canceled': 0, 'replaced': 0}      # 一致 -> 不动


def test_reconcile_replaces_missing_by_order_id():
    ex, gx = _setup(100.0)
    gid = gx.open('fake', SYM, GP)
    # 交易所侧撤掉一个挂单（库里仍记 open）-> reconcile 应补回
    victim = ex.fetch_open_orders(SYM)[0]
    ex._open.pop(victim.id, None)
    r = Reconciler(gx).reconcile_open_orders(gid, SYM)
    assert r['replaced'] == 1


def test_reconcile_cancels_orphan_by_order_id():
    ex, gx = _setup(100.0)
    gid = gx.open('fake', SYM, GP)
    # 交易所多出一个我方不认的挂单 -> reconcile 应撤
    ex.create_limit_order(SYM, 'buy', 90.0, 1.0, client_oid='orphan')
    r = Reconciler(gx).reconcile_open_orders(gid, SYM)
    assert r['canceled'] == 1
```

- [ ] **Step 2: 跑测试确认红** `... pytest tests/execution/test_reconcile_orderid.py -q` → 视实现，replace/cancel 计数与新口径不符则 FAIL（先看红再改）。

- [ ] **Step 3: 实现** 把 `reconciler.py` `reconcile_open_orders` 改为按 order id：

```python
    def reconcile_open_orders(self, grid_id, symbol):
        ex = self.ex
        expected = {o.exchange_order_id: o for o in ex.orders.list_open_by_grid(grid_id)
                    if o.exchange_order_id}
        on_exchange = {o.id: o for o in ex.adapter.fetch_open_orders(symbol)}

        canceled = 0
        for oid, o in on_exchange.items():
            if oid not in expected:
                ex.adapter.cancel_order(symbol, o.id)
                canceled += 1

        replaced = 0
        for oid, go in expected.items():
            if oid not in on_exchange:
                order = ex.adapter.create_limit_order(symbol, go.side, go.price, go.size,
                                                      post_only=False, client_oid=go.client_oid)
                ex.orders.upsert(GridOrder(client_oid=go.client_oid, grid_id=grid_id,
                                           line_index=go.line_index, side=go.side, price=go.price,
                                           size=go.size, status='open',
                                           exchange_order_id=getattr(order, 'id', None)))
                replaced += 1
        return {'canceled': canceled, 'replaced': replaced}
```

并把模块 docstring 的「按 client_oid 对账」改为「按 exchange order id 对账」。

- [ ] **Step 4: 跑测试确认绿 + 回归** 新测试 PASS；`TZ=Asia/Shanghai .venv/bin/python -m pytest` 全绿（含原 test_reconciler 金标）。

- [ ] **Step 5: 提交** `git commit -m "feat(execution): reconcile open orders by exchange order id (P4x)"`

---

## Self-Review

- **覆盖**：fill→line（Task 3）+ 对账（Task 4）改 order id；下单省略 HL 非法 cloid（Task 2）；Trade 带 order_id（Task 1）。
- **向后兼容**：FakeExchange/OKX 下 encode_cloid 原样、order_id=订单号，现有金标测试回归即主证；新测试专证 order-id 路径（client_oid 不可解析仍能映射）。
- **幂等保留**：成交去重仍靠 grid_fills.trade_id（未动）。
- **类型一致**：`Trade.order_id: Optional[str]`；sync/reconcile 用 `exchange_order_id`↔`order.id`/`trade.order_id`。
