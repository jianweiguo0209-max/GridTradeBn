# 交易所解耦重构 P3c 实现计划（自管理挂单网格执行器 GridExecutor）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `gridtrade/execution/grid_executor.py` 的 `GridExecutor`：自管理挂单式网格的生命周期（开网/同步补单/平网），驱动 `ExchangeAdapter` + P2 状态层仓储 + `LiveEquity`；并给适配器新增账户级资金费流水查询 `fetch_funding_payments`。一切针对 `FakeExchange` 离线 TDD。

**Architecture:** 状态机编排。`open()` 用 `core.grid_engine.grid_order_info` 算几何 → 中性底仓市价买 → 入场价上方挂限价卖、下方挂限价买（`client_oid = "{grid_id}:{line}:{seq}"`）→ 持久化 grid/grid_orders/accounting，状态 PENDING→OPENING→ACTIVE。`sync()` 拉自上次游标后的成交 → 喂 LiveEquity → 每条成交线补对侧单 → 落库订单 + accounting 快照。`close(reason)` 撤全部 + 市价 reduce 平净仓 + 落 order_records + 状态 CLOSING→CLOSED。**交易所是订单/持仓真相源；client_oid 确定性映射到 (grid,line) 供对账。**

**Tech Stack:** Python 3.9、ccxt 4.5.61、SQLAlchemy 2.0、pandas 1.3.5、pytest。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（执行器编排语义、补单规则、持久化次序、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；ccxt 4.5.61；SQLAlchemy 2.0；pandas 1.3.5。
- `gridtrade/execution/grid_executor.py` 可 import `gridtrade/core/`、`gridtrade/exchanges/`、`gridtrade/state/`、`gridtrade/execution/live_equity.py`；**不得**硬编码任何具体交易所（只经 `ExchangeAdapter` 接口）。
- **资金费记账口径（已确认）**：用交易所实际扣款流水。适配器新增 `fetch_funding_payments(symbol, since_ms=None) -> List[FundingPayment]`，`FundingPayment(ts, amount)`，**amount>0 表示支付（净值下降）**，由适配器把 ccxt `fetch_funding_history` 的原始符号统一成此约定。
- `client_oid` 格式：`f"{grid_id}:{line_index}:{seq}"`，`seq` 为每个 GridExecutor 实例自增计数（保证补单重挂时 client_oid 唯一）。`grid_orders.line_index` 记录网格线序号。
- 中性初始化：开网即市价买入 = 入场价**严格上方**所有网格线的 `order_num` 之和（复刻 OKX 中性网格底仓，与 `core.grid_engine.simulate_grid_engine(neutral_init=True)` 同口径）。市价单 `client_oid = f"{grid_id}:init:0"`，不计入网格线挂单。
- 补单规则：某网格线 `i` 的卖单成交 → 在线 `i-1` 挂买单；买单成交 → 在线 `i+1` 挂卖单（经典网格补位；越界则不补）。
- 持久化次序（崩溃安全）：先建 grid(PENDING) → 建 accounting → 下单边下边 upsert grid_orders → 全部就绪后 transition→ACTIVE。平网：先 transition→CLOSING → 撤单/平仓 → 落 record → transition→CLOSED。
- 不修改 `account_0/`、`backtest/`、已有的 `gridtrade/{core,state}/` 与 `gridtrade/execution/live_equity.py`（只新增 grid_executor.py；exchanges 仅**新增** funding-payments，不改既有方法）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。

---

## 文件结构（本计划新建/修改）

```
gridtrade/exchanges/
  base.py            # 修改：+ FundingPayment 数据类 + abstract fetch_funding_payments
  ccxt_adapter.py    # 修改：+ fetch_funding_payments（map ccxt fetch_funding_history）
  fake.py            # 修改：+ seed_funding_payments 钩子 + fetch_funding_payments
gridtrade/execution/
  grid_executor.py   # 新增：GridExecutor（open/sync/close）
tests/exchanges/
  test_funding_payments.py   # 新增
tests/execution/
  test_grid_executor.py      # 新增
```

---

### Task 1: 适配器账户资金费流水 fetch_funding_payments

**Files:**
- Modify: `gridtrade/exchanges/base.py`
- Modify: `gridtrade/exchanges/ccxt_adapter.py`
- Modify: `gridtrade/exchanges/fake.py`
- Create: `tests/exchanges/test_funding_payments.py`

**Interfaces:**
- Produces:
  - `gridtrade.exchanges.base.FundingPayment`（dataclass: `ts: int`(ms), `amount: float`，amount>0=支付）。
  - `ExchangeAdapter.fetch_funding_payments(self, symbol, since_ms=None) -> List[FundingPayment]`（抽象方法）。
  - `CcxtAdapter.fetch_funding_payments`：调用 `self.client.fetch_funding_history(self.to_native(symbol), since=since_ms)`，把每条的 `timestamp`→ts、`-float(entry['amount'])`→amount（ccxt 约定 amount 负=支付，故取负使"支付为正"），过滤 `ts >= since_ms`，按 ts 升序。
  - `FakeExchange.seed_funding_payments(self, symbol, payments)`（payments: list of (ts, amount)，amount>0=支付）+ `fetch_funding_payments` 返回注入值（按 since_ms 过滤、ts 升序）。

- [ ] **Step 1: 写测试**

Create `tests/exchanges/test_funding_payments.py`:

```python
from gridtrade.exchanges.base import Instrument


def _fake():
    from gridtrade.exchanges.fake import FakeExchange
    return FakeExchange(instruments=[Instrument('BTC/USDT:USDT', 0.1, 0.001, 0.001, 'live', 0)])


def test_fake_seed_and_fetch_funding_payments():
    from gridtrade.exchanges.base import FundingPayment
    ex = _fake()
    ex.seed_funding_payments('BTC/USDT:USDT', [(1000, 0.5), (2000, -0.3), (3000, 0.2)])
    out = ex.fetch_funding_payments('BTC/USDT:USDT')
    assert all(isinstance(p, FundingPayment) for p in out)
    assert [(p.ts, p.amount) for p in out] == [(1000, 0.5), (2000, -0.3), (3000, 0.2)]


def test_fake_funding_payments_since_filter():
    ex = _fake()
    ex.seed_funding_payments('BTC/USDT:USDT', [(1000, 0.5), (2000, -0.3), (3000, 0.2)])
    out = ex.fetch_funding_payments('BTC/USDT:USDT', since_ms=2000)
    assert [(p.ts, p.amount) for p in out] == [(2000, -0.3), (3000, 0.2)]


def test_ccxt_funding_payments_sign_and_mapping():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    from gridtrade.exchanges.base import FundingPayment

    class FakeClient:
        def fetch_funding_history(self, symbol, since=None, limit=None, params=None):
            # ccxt 约定：amount 负=支付，正=收取
            return [{'timestamp': 1000, 'amount': -0.5, 'symbol': symbol},
                    {'timestamp': 2000, 'amount': 0.3, 'symbol': symbol}]

    a = CcxtAdapter(FakeClient(), name='ccxt')
    out = a.fetch_funding_payments('BTC/USDT:USDT')
    assert out == [FundingPayment(ts=1000, amount=0.5), FundingPayment(ts=2000, amount=-0.3)]


def test_adapter_declares_fetch_funding_payments_abstract():
    from gridtrade.exchanges.base import ExchangeAdapter
    assert 'fetch_funding_payments' in ExchangeAdapter.__abstractmethods__
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_funding_payments.py -v`
Expected: FAIL（`ImportError: cannot import name 'FundingPayment'` 或 AttributeError）。

- [ ] **Step 3: 实现**

在 `gridtrade/exchanges/base.py`：在其它 dataclass 旁新增
```python
@dataclass
class FundingPayment:
    ts: int       # 毫秒
    amount: float  # >0 表示支付（净值下降）
```
并在 `ExchangeAdapter` 内新增抽象方法（放在 `exchange_status` 之后、`fetch_mark_ohlcv` 之前）：
```python
    @abstractmethod
    def fetch_funding_payments(self, symbol: str,
                               since_ms: Optional[int] = None) -> List[FundingPayment]:
        """账户级资金费扣款流水；amount>0 表示支付。按 ts 升序。"""
```

在 `gridtrade/exchanges/ccxt_adapter.py`：import `FundingPayment`，并新增方法（放在 `exchange_status` 之后）：
```python
    def fetch_funding_payments(self, symbol, since_ms=None):
        rows = self.client.fetch_funding_history(self.to_native(symbol), since=since_ms)
        out = []
        for r in rows:
            ts = int(r['timestamp'])
            if since_ms is not None and ts < since_ms:
                continue
            # ccxt 约定 amount 负=支付；统一成"支付为正"
            out.append(FundingPayment(ts=ts, amount=-float(r['amount'])))
        out.sort(key=lambda p: p.ts)
        return out
```

在 `gridtrade/exchanges/fake.py`：在 `__init__` 增加 `self._funding_payments = {}`；新增钩子与实现：
```python
    def seed_funding_payments(self, symbol, payments):
        self._funding_payments[symbol] = [tuple(p) for p in payments]

    def fetch_funding_payments(self, symbol, since_ms=None):
        from gridtrade.exchanges.base import FundingPayment
        rows = self._funding_payments.get(symbol, [])
        out = [FundingPayment(ts=int(ts), amount=float(amt)) for ts, amt in rows
               if since_ms is None or int(ts) >= since_ms]
        out.sort(key=lambda p: p.ts)
        return out
```
（`FakeExchange` 因实现了新抽象方法仍可实例化。）

- [ ] **Step 4: 运行确认通过 + 既有适配器测试不回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/ -v`
Expected: PASS（新增 4 + 既有 exchanges 测试全绿；注意 FakeExchange/CcxtAdapter/Okx/HL 仍可实例化）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/base.py gridtrade/exchanges/ccxt_adapter.py gridtrade/exchanges/fake.py tests/exchanges/test_funding_payments.py
git commit -m "feat(exchanges): add account funding-payment query (fetch_funding_payments)"
```

---

### Task 2: GridExecutor.open（几何 + 中性底仓 + 挂网格 + 持久化）

**Files:**
- Create: `gridtrade/execution/grid_executor.py`
- Create: `tests/execution/test_grid_executor.py`

**Interfaces:**
- Consumes: `ExchangeAdapter`、`StateStore` + `GridRepository`/`OrderRepository`/`AccountingRepository`、`LiveEquity`、`core.grid_engine.grid_order_info`、`core.state.models.Grid/GridOrder`。
- Produces: `gridtrade.execution.grid_executor.GridExecutor`：
  - `__init__(self, adapter, store, *, cap, leverage, fee=0.0002, c_rate_taker=0.0005, max_rate=0.68)`：内部建 `GridRepository(store)/OrderRepository(store)/AccountingRepository(store)`。
  - `open(self, exchange, symbol, grid_params, *, offset=0, tag='') -> str`：建网，返回 grid_id。
    - 计算 `gi = grid_order_info(cap, leverage, low_price, high_price, grid_count, stop_low_price, stop_high_price, max_rate=max_rate)`；若 None 抛 `RuntimeError('建网失败：保证金不足')`。
    - `entry = adapter.fetch_price(symbol)`。
    - `GridRepository.create(Grid(..., status=PENDING, entry_price=entry, low/high/stop/grid_count/order_num/leverage/cap, offset, tag))` → grid_id。`AccountingRepository.init(grid_id)`。
    - transition→OPENING。中性底仓：`above = [p for p in price_array if p > entry]`；`adapter.create_market_order(symbol,'buy', order_num*len(above), client_oid=f'{grid_id}:init:0')`。
    - 逐线挂单：line i 价 p，若 `p>entry` 挂 sell、`p<entry` 挂 buy（`p==entry` 跳过），`client_oid=f'{grid_id}:{i}:{seq()}'`；每单 `OrderRepository.upsert(GridOrder(client_oid, grid_id, line_index=i, side, price=p, size=order_num, status='open', exchange_order_id=order.id))`。
    - transition→ACTIVE。返回 grid_id。
  - 暴露 `live` 属性（`Dict[grid_id, LiveEquity]`），开网时为该 grid 建一个 `LiveEquity(cap, fee, c_rate_taker, entry_price=entry)`。

- [ ] **Step 1: 写测试**

Create `tests/execution/test_grid_executor.py`:

```python
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
from gridtrade.state.models import ACTIVE


SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    store = StateStore.in_memory(); store.create_all()
    from gridtrade.execution.grid_executor import GridExecutor
    ex_ = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    return ex, store, ex_


def test_open_places_grid_and_neutral_inventory():
    ex, store, gx = _setup(price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP, offset=0, tag='t0')
    # 网格记录 ACTIVE
    from gridtrade.state.grids import GridRepository
    g = GridRepository(store).get(gid)
    assert g.status == ACTIVE and g.entry_price == 100.0
    # 中性底仓：入场价上方 4 条线 × order_num
    on = g.order_num
    pos = ex.fetch_positions(SYM)
    assert abs(pos.net_size - on * 4) < 1e-6
    # 9 条线，entry 不在线上 → 9 个挂单
    opens = ex.fetch_open_orders(SYM)
    assert len(opens) == 9
    sells = [o for o in opens if o.side == 'sell']
    buys = [o for o in opens if o.side == 'buy']
    assert len(sells) == 4 and len(buys) == 5


def test_open_persists_orders_with_client_oid():
    ex, store, gx = _setup()
    gid = gx.open(ex_exchange_name(), SYM, GP)
    from gridtrade.state.orders import OrderRepository
    rows = OrderRepository(store).list_by_grid(gid)
    assert len(rows) == 9
    assert all(r.client_oid.startswith(f'{gid}:') for r in rows)
    assert all(r.status == 'open' for r in rows)


def test_open_undercapitalized_raises():
    import pytest
    ex, store, _ = _setup()
    from gridtrade.execution.grid_executor import GridExecutor
    # min_amount 极大 → 每格量被向下取整到 0 → grid_order_info 返回 None → 建网失败
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, min_amount=1e9)
    with pytest.raises(RuntimeError):
        gx.open(ex_exchange_name(), SYM, GP)


def ex_exchange_name():
    return 'fake'
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.grid_executor`）。

- [ ] **Step 3: 写 grid_executor.py（open 部分）**

Create `gridtrade/execution/grid_executor.py`:

```python
"""GridExecutor：自管理挂单网格生命周期（开网/同步补单/平网）。
驱动 ExchangeAdapter + 状态层仓储 + LiveEquity。交易所为订单/持仓真相源；
client_oid='{grid_id}:{line}:{seq}' 确定性映射网格线，供对账。
"""
import itertools

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.live_equity import LiveEquity
from gridtrade.state.accounting import AccountingRepository
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import (ACTIVE, Grid, GridOrder, OPENING, now_ms)
from gridtrade.state.orders import OrderRepository


class GridExecutor:
    def __init__(self, adapter, store, *, cap, leverage, fee=0.0002,
                 c_rate_taker=0.0005, max_rate=0.68, min_amount=0.0):
        self.adapter = adapter
        self.grids = GridRepository(store)
        self.orders = OrderRepository(store)
        self.accounting = AccountingRepository(store)
        self.cap = float(cap)
        self.leverage = float(leverage)
        self.fee = float(fee)
        self.c_rate_taker = float(c_rate_taker)
        self.max_rate = float(max_rate)
        self.min_amount = float(min_amount)
        self.live = {}        # grid_id -> LiveEquity
        self._geom = {}       # grid_id -> dict(price_array, order_num)
        self._seq = {}        # grid_id -> itertools.count

    def _next_oid(self, grid_id, line_index):
        return '%s:%d:%d' % (grid_id, line_index, next(self._seq[grid_id]))

    def open(self, exchange, symbol, grid_params, *, offset=0, tag=''):
        gi = grid_order_info(self.cap, self.leverage, grid_params['low_price'],
                             grid_params['high_price'], int(grid_params['grid_count']),
                             grid_params['stop_low_price'], grid_params['stop_high_price'],
                             min_amount=self.min_amount, max_rate=self.max_rate)
        if gi is None:
            raise RuntimeError('建网失败：保证金不足')
        price_array = [float(p) for p in gi['价格序列']]
        order_num = float(gi['每笔数量'])
        entry = float(self.adapter.fetch_price(symbol))

        grid = self.grids.create(Grid(
            id='', exchange=exchange, symbol=symbol, status='PENDING', offset=offset, tag=tag,
            entry_price=entry, low_price=grid_params['low_price'], high_price=grid_params['high_price'],
            stop_low_price=grid_params['stop_low_price'], stop_high_price=grid_params['stop_high_price'],
            grid_count=int(grid_params['grid_count']), order_num=order_num,
            leverage=self.leverage, cap=self.cap))
        gid = grid.id
        self.accounting.init(gid)
        self._geom[gid] = {'price_array': price_array, 'order_num': order_num}
        self._seq[gid] = itertools.count()
        self.live[gid] = LiveEquity(self.cap, self.fee, self.c_rate_taker, entry_price=entry)

        self.grids.transition_status(gid, OPENING, expected_version=grid.version)

        # 中性底仓：入场价上方线数 × 每格量，市价买
        above = [p for p in price_array if p > entry]
        if above:
            self.adapter.create_market_order(symbol, 'buy', order_num * len(above),
                                             client_oid='%s:init:0' % gid)

        # 逐线挂限价单
        for i, p in enumerate(price_array):
            if p > entry:
                side = 'sell'
            elif p < entry:
                side = 'buy'
            else:
                continue
            oid = self._next_oid(gid, i)
            order = self.adapter.create_limit_order(symbol, side, p, order_num,
                                                    post_only=False, client_oid=oid)
            self.orders.upsert(GridOrder(client_oid=oid, grid_id=gid, line_index=i,
                                         side=side, price=p, size=order_num, status='open',
                                         exchange_order_id=getattr(order, 'id', None)))

        g2 = self.grids.get(gid)
        self.grids.transition_status(gid, ACTIVE, expected_version=g2.version)
        return gid
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/grid_executor.py tests/execution/test_grid_executor.py
git commit -m "feat(execution): GridExecutor.open (geometry, neutral inventory, place grid, persist)"
```

---

### Task 3: GridExecutor.sync（检测成交 → LiveEquity + 补单 + 落库）

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`
- Modify: `tests/execution/test_grid_executor.py`

**Interfaces:**
- Produces（在 GridExecutor 上新增）：
  - `sync(self, grid_id, symbol) -> dict`：拉自上次游标后的成交，更新记账与补单。返回 `{new_fills, snapshot}`。
    - 维护 `self._trade_cursor[grid_id]`（初始 0）。`trades = adapter.fetch_my_trades(symbol, since_ms=cursor)`，只取 `client_oid` 属于本 grid（前缀 `f'{grid_id}:'`）且非 `:init:` 的成交；按 ts 升序。
    - 对每条新成交：`self.live[grid_id].record_fill(price, side, size, ts)`；标记对应订单 `status='closed'`（`OrderRepository.upsert`）；补对侧单：卖成交→在 `line_index-1` 挂 buy；买成交→在 `line_index+1` 挂 sell（越界不补）；补单用 `_next_oid` 并 `upsert` 新 GridOrder(status='open')。
    - 资金费：`pays = adapter.fetch_funding_payments(symbol, since_ms=self._funding_cursor[grid_id])`；逐条 `self.live[grid_id].add_funding(p.amount)`，推进 `_funding_cursor`。
    - 推进 `_trade_cursor` 到最后成交 ts+1。
    - `snap = self.live[grid_id].snapshot(adapter.fetch_price(symbol))`；把 `net_position/avg_price/realized_pnl/fee_paid/funding_paid` 存入 accounting（读-改-`AccountingRepository.save`，乐观锁），并 `accounting.bump_peak(grid_id, snap['pnl_ratio'])`。返回 `{'new_fills': len(new), 'snapshot': snap}`。
  - `__init__` 增加 `self._trade_cursor = {}`、`self._funding_cursor = {}`（开网时置 0）。

- [ ] **Step 1: 追加测试**

在 `tests/execution/test_grid_executor.py` 追加：

```python
def test_sync_records_fill_and_replenishes():
    ex, store, gx = _setup(price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    before_open = len(ex.fetch_open_orders(SYM))
    ex.set_price(SYM, 100.6)   # 触发 line 5 卖单成交（100.4812）
    res = gx.sync(gid, SYM)
    assert res['new_fills'] == 1
    # 补单：卖成交后总挂单数不变（撤一卖、补一买）
    assert len(ex.fetch_open_orders(SYM)) == before_open
    # LiveEquity 记录了该成交，净仓下降一格量
    from gridtrade.state.grids import GridRepository
    on = GridRepository(store).get(gid).order_num
    assert abs(ex.fetch_positions(SYM).net_size - on * 3) < 1e-6
    # accounting 落了快照
    acc = gx.accounting.get(gid)
    assert acc is not None and abs(acc.net_position - on * 3) < 1e-6


def test_sync_funding_payments_accumulate():
    ex, store, gx = _setup(price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    ex.seed_funding_payments(SYM, [(10_000, 1.0)])   # 支付 1 USDT
    gx.sync(gid, SYM)
    acc = gx.accounting.get(gid)
    assert abs(acc.funding_paid - 1.0) < 1e-9


def test_sync_idempotent_no_new_fills():
    ex, store, gx = _setup(price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    ex.set_price(SYM, 100.6)
    gx.sync(gid, SYM)
    res2 = gx.sync(gid, SYM)   # 第二次无新成交
    assert res2['new_fills'] == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor.py -k sync -v`
Expected: FAIL（`AttributeError: 'GridExecutor' object has no attribute 'sync'`）。

- [ ] **Step 3: 实现 sync**

在 `GridExecutor.__init__` 末尾加：
```python
        self._trade_cursor = {}
        self._funding_cursor = {}
```
在 `open` 内（建 live 后）加：`self._trade_cursor[gid] = 0; self._funding_cursor[gid] = 0`。

在类内新增（import 顶部补 `from gridtrade.state.models import now_ms` 已有）：
```python
    def sync(self, grid_id, symbol):
        geom = self._geom[grid_id]
        price_array = geom['price_array']
        order_num = geom['order_num']
        cursor = self._trade_cursor.get(grid_id, 0)
        trades = self.adapter.fetch_my_trades(symbol, since_ms=cursor)
        prefix = '%s:' % grid_id
        new = [t for t in trades
               if t.client_oid.startswith(prefix) and ':init:' not in t.client_oid]
        new.sort(key=lambda t: t.ts)

        for t in new:
            line_index = int(t.client_oid.split(':')[1])
            self.live[grid_id].record_fill(t.price, t.side, t.size, t.ts)
            # 标记成交订单 closed
            self.orders.upsert(GridOrder(client_oid=t.client_oid, grid_id=grid_id,
                                         line_index=line_index, side=t.side, price=t.price,
                                         size=t.size, status='closed'))
            # 补对侧单
            opp_line = line_index - 1 if t.side == 'sell' else line_index + 1
            if 0 <= opp_line < len(price_array):
                opp_side = 'buy' if t.side == 'sell' else 'sell'
                p = price_array[opp_line]
                oid = self._next_oid(grid_id, opp_line)
                order = self.adapter.create_limit_order(symbol, opp_side, p, order_num,
                                                        post_only=False, client_oid=oid)
                self.orders.upsert(GridOrder(client_oid=oid, grid_id=grid_id, line_index=opp_line,
                                             side=opp_side, price=p, size=order_num, status='open',
                                             exchange_order_id=getattr(order, 'id', None)))

        if new:
            self._trade_cursor[grid_id] = new[-1].ts + 1

        # 资金费流水
        fcur = self._funding_cursor.get(grid_id, 0)
        pays = self.adapter.fetch_funding_payments(symbol, since_ms=fcur)
        for p in pays:
            self.live[grid_id].add_funding(p.amount)
        if pays:
            self._funding_cursor[grid_id] = pays[-1].ts + 1

        snap = self.live[grid_id].snapshot(float(self.adapter.fetch_price(symbol)))
        acc = self.accounting.get(grid_id)
        if acc is not None:
            acc.realized_pnl = snap['realized_pnl']
            acc.fee_paid = snap['fee_paid']
            acc.funding_paid = snap['funding_paid']
            acc.net_position = snap['net_position']
            acc.avg_price = snap['avg_price']
            self.accounting.save(acc)
            self.accounting.bump_peak(grid_id, snap['pnl_ratio'])
        return {'new_fills': len(new), 'snapshot': snap}
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor.py -v`
Expected: PASS（6 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/grid_executor.py tests/execution/test_grid_executor.py
git commit -m "feat(execution): GridExecutor.sync (detect fills, replenish, fund/account snapshot)"
```

---

### Task 4: GridExecutor.close（撤单 + 市价平仓 + 落 record + 状态）+ 全套回归

**Files:**
- Modify: `gridtrade/execution/grid_executor.py`
- Modify: `tests/execution/test_grid_executor.py`

**Interfaces:**
- Consumes: `gridtrade.state.records.RecordRepository`、`gridtrade.state.models.Record/CLOSING/CLOSED`。
- Produces（在 GridExecutor 上新增）：
  - `__init__` 增加 `self.records = RecordRepository(store)`。
  - `close(self, grid_id, symbol, reason) -> dict`：
    - 读 grid；`transition_status(grid_id, CLOSING, expected_version=grid.version)`。
    - `self.adapter.cancel_all(symbol)`；把本 grid 所有 open 订单 `OrderRepository` 标 `status='canceled'`（遍历 `list_open_by_grid`，逐条 upsert canceled）。
    - 平净仓：`pos = adapter.fetch_positions(symbol)`；若 `abs(pos.net_size) > 0`，市价 reduce（`side='sell' if net>0 else 'buy'`，size=abs(net)，reduce_only=True，client_oid=f'{grid_id}:close:0'）。
    - `snap = self.live[grid_id].snapshot(adapter.fetch_price(symbol))`；`RecordRepository.add(Record(id='', grid_id, exchange=grid.exchange, symbol, tag=grid.tag, offset=grid.offset, opened_at=grid.created_at, closed_at=now_ms(), sz=cap, total_pnl=snap['pnl_ratio']*cap, pnl_ratio=snap['pnl_ratio'], exit_reason=reason))`。
    - `g2=get; transition_status(grid_id, CLOSED, expected_version=g2.version)`。返回 `{'reason': reason, 'pnl_ratio': snap['pnl_ratio']}`。

- [ ] **Step 1: 追加测试**

```python
def test_close_cancels_orders_flattens_and_records():
    from gridtrade.state.models import CLOSED
    ex, store, gx = _setup(price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    out = gx.close(gid, SYM, '固定止损')
    assert out['reason'] == '固定止损'
    # 所有挂单已撤
    assert ex.fetch_open_orders(SYM) == []
    # 净仓已平
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9
    # 网格 CLOSED，槽位释放
    from gridtrade.state.grids import GridRepository
    assert GridRepository(store).get(gid).status == CLOSED
    assert GridRepository(store).get_active_by_symbol('fake', SYM) is None
    # 留下一条 record
    recs = gx.records.list_by_grid(gid)
    assert len(recs) == 1 and recs[0].exit_reason == '固定止损'


def test_close_then_reopen_same_symbol_ok():
    ex, store, gx = _setup(price=100.0)
    gid = gx.open(ex_exchange_name(), SYM, GP)
    gx.close(gid, SYM, '手动停止')
    gid2 = gx.open(ex_exchange_name(), SYM, GP)   # 槽位已释放，可再开
    assert gid2 != gid
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor.py -k close -v`
Expected: FAIL（`AttributeError: ... 'close'`）。

- [ ] **Step 3: 实现 close**

import 顶部补：`from gridtrade.state.models import CLOSED, CLOSING, Record`；`from gridtrade.state.records import RecordRepository`。`__init__` 加 `self.records = RecordRepository(store)`。新增：
```python
    def close(self, grid_id, symbol, reason):
        grid = self.grids.get(grid_id)
        self.grids.transition_status(grid_id, CLOSING, expected_version=grid.version)
        self.adapter.cancel_all(symbol)
        for o in self.orders.list_open_by_grid(grid_id):
            self.orders.upsert(GridOrder(client_oid=o.client_oid, grid_id=grid_id,
                                         line_index=o.line_index, side=o.side, price=o.price,
                                         size=o.size, status='canceled'))
        pos = self.adapter.fetch_positions(symbol)
        if abs(pos.net_size) > 0:
            side = 'sell' if pos.net_size > 0 else 'buy'
            self.adapter.create_market_order(symbol, side, abs(pos.net_size),
                                             reduce_only=True, client_oid='%s:close:0' % grid_id)
        snap = self.live[grid_id].snapshot(float(self.adapter.fetch_price(symbol)))
        self.records.add(Record(id='', grid_id=grid_id, exchange=grid.exchange, symbol=symbol,
                                tag=grid.tag, offset=grid.offset, opened_at=grid.created_at,
                                closed_at=now_ms(), sz=self.cap, total_pnl=snap['pnl_ratio'] * self.cap,
                                pnl_ratio=snap['pnl_ratio'], exit_reason=reason))
        g2 = self.grids.get(grid_id)
        self.grids.transition_status(grid_id, CLOSED, expected_version=g2.version)
        return {'reason': reason, 'pnl_ratio': snap['pnl_ratio']}
```

- [ ] **Step 4: 运行确认通过 + 全套回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_grid_executor.py -v`
Expected: PASS（8 passed）。

Run（全仓回归）: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全 PASS（既有 73 + Task1 的 4 + 执行器 8 ≈ 85）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/grid_executor.py tests/execution/test_grid_executor.py
git commit -m "feat(execution): GridExecutor.close (cancel, flatten, record, CLOSED)"
```

---

## 完成判定（P3c）

- `pytest -q` 全绿：适配器资金费流水；GridExecutor 开网(几何+中性底仓+挂单+持久化)、同步(成交→记账+补单+资金费)、平网(撤单+平仓+落 record+释放槽位+可重开)。
- 全程针对 FakeExchange + 内存 StateStore + LiveEquity，无外部网络。
- `gridtrade/execution/grid_executor.py` 只经 `ExchangeAdapter` 接口访问交易所，无硬编码交易所。

## 后续（P3d，不在本计划内）

`execution/reconciler.py`（重启对账自愈：从 state 载 ACTIVE 网格意图，拉交易所 open_orders/position/my_trades，diff 补缺挂单/撤孤儿单/用 my_trades `replay` 重建 LiveEquity 与 accounting）+ 监控层（组合 `sync` + `evaluate_exit`[资金费已知 0 传 0.0 非 None] → 触发 `close`）。P3d 起接入 P4 运行时（scheduler/monitor 机 + fly.io）。
