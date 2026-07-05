# AccountSnapshot 账户级批量取数 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** monitor 读路径每轮 5 次账户级调用（≈64 权重、与格数无关）替代逐格 ~6 次（≈84 权重/格），根治 429。

**Architecture:** 轮首主线程 `build_account_snapshot`（5 次账户级调用，走 ResilientAdapter 电路），不可变 `AccountSnapshot` 传给各并行单元；`sync`/`reconcile_*` 加可选 `snapshot=None` 参数（None=现状逐格取数，完整保留）。构建失败=整轮跳过（用户决定）。

**Tech Stack:** Python 3.9、SQLAlchemy、ccxt(hyperliquid)、pytest。

**Spec:** `docs/superpowers/specs/2026-07-06-account-snapshot-batch-reads-design.md`

## Global Constraints

- 语义等价：snapshot 与 None 双路径终态一致（差分测试钉死）；成交归属仍靠 by_oid，逐笔 add_if_new 去重不变。
- 写路径零改动（补单/撤单/平仓/保险丝重挂仍逐格、走全局写锁）。
- `fetch_prices_all` 归 market_read 电路，其余 4 个 `_all` 归 account_read。
- 快照构建失败 → cycle 整轮跳过单元（日志 `[monitor] snapshot failed`），心跳/指令/equity 照常。
- 测试跑法：`.venv/bin/python -m pytest <path> -q`；每 Task 一 commit。
- 现有全套（~570）必须保持全绿；语义确需校准的旧测试（chaos 读故障、halt 桩）在 Task 7 内显式校准并注释理由。

---

### Task 1: base.py 账户级默认实现（逐 symbol 合成）

**Files:**
- Modify: `gridtrade/exchanges/base.py`（`fetch_24h_quote_volumes` 后追加）
- Test: `tests/exchanges/test_account_batch_base.py`（新建）

**Interfaces:**
- Produces（后续所有 Task 依赖的签名）:
  - `fetch_my_trades_all(symbols, since_ms=None) -> List[Trade]`（按 ts 升序）
  - `fetch_open_orders_all(symbols) -> List[Order]`
  - `fetch_positions_all(symbols) -> Dict[str, float]`（symbol→带符号 net_size）
  - `fetch_prices_all(symbols) -> Dict[str, float]`
  - `fetch_funding_payments_all(symbols, since_ms=None) -> Dict[str, List[FundingPayment]]`

- [ ] **Step 1: 写失败测试**（FakeExchange 差分：默认 `_all` == 逐 symbol 手工合成）

```python
# tests/exchanges/test_account_batch_base.py
"""base 默认账户级方法 = 逐 symbol 合成（任何交易所天然可用）。差分等价测试。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'


def _fake():
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=100.0)
    ex.set_price(BTC, 100.0); ex.set_price(ETH, 200.0)
    ex.create_limit_order(BTC, 'buy', 99.0, 1.0, client_oid='b1')
    ex.create_limit_order(ETH, 'sell', 201.0, 2.0, client_oid='e1')
    ex.set_price(BTC, 98.5)          # 触发 BTC 买单成交 → trades/positions 非空
    return ex


def test_trades_all_equals_per_symbol_merged_sorted():
    ex = _fake()
    manual = sorted(ex.fetch_my_trades(BTC) + ex.fetch_my_trades(ETH), key=lambda t: t.ts)
    assert ex.fetch_my_trades_all([BTC, ETH]) == manual
    assert manual                      # 场景确实有成交（防空转真空）


def test_open_orders_all_equals_per_symbol():
    ex = _fake()
    got = {o.id for o in ex.fetch_open_orders_all([BTC, ETH])}
    manual = {o.id for o in ex.fetch_open_orders(BTC) + ex.fetch_open_orders(ETH)}
    assert got == manual and manual


def test_positions_prices_funding_all_equal_per_symbol():
    ex = _fake()
    assert ex.fetch_positions_all([BTC, ETH]) == {
        BTC: ex.fetch_positions(BTC).net_size, ETH: ex.fetch_positions(ETH).net_size}
    assert ex.fetch_prices_all([BTC, ETH]) == {BTC: 98.5, ETH: 200.0}
    assert ex.fetch_funding_payments_all([BTC, ETH]) == {
        BTC: ex.fetch_funding_payments(BTC), ETH: ex.fetch_funding_payments(ETH)}
```

- [ ] **Step 2: 跑测试确认失败**：`.venv/bin/python -m pytest tests/exchanges/test_account_batch_base.py -q` → FAIL（AttributeError: fetch_my_trades_all）

- [ ] **Step 3: 实现**（base.py `fetch_24h_quote_volumes` 之后追加）

```python
    # ---- 账户级批量读（monitor 快照用）：默认逐 symbol 合成；HL 等账户级端点交易所覆写 ----
    def fetch_my_trades_all(self, symbols, since_ms: Optional[int] = None) -> List[Trade]:
        out: List[Trade] = []
        for s in symbols:
            out.extend(self.fetch_my_trades(s, since_ms=since_ms))
        out.sort(key=lambda t: t.ts)
        return out

    def fetch_open_orders_all(self, symbols) -> List[Order]:
        out: List[Order] = []
        for s in symbols:
            out.extend(self.fetch_open_orders(s))
        return out

    def fetch_positions_all(self, symbols) -> dict:
        return {s: float(self.fetch_positions(s).net_size) for s in symbols}

    def fetch_prices_all(self, symbols) -> dict:
        return {s: float(self.fetch_price(s)) for s in symbols}

    def fetch_funding_payments_all(self, symbols, since_ms: Optional[int] = None) -> dict:
        return {s: self.fetch_funding_payments(s, since_ms=since_ms) for s in symbols}
```

- [ ] **Step 4: 跑测试确认通过**；顺带 `tests/exchanges/ -q` 全绿
- [ ] **Step 5: Commit** `feat(exchanges): base 账户级批量读默认实现（逐 symbol 合成）`

---

### Task 2: HyperliquidAdapter 原生账户级实现

**Files:**
- Modify: `gridtrade/exchanges/ccxt_adapter.py`（抽 `_to_trade` 供复用；`fetch_my_trades` 改用它）
- Modify: `gridtrade/exchanges/hyperliquid.py`（5 个覆写 + `_coin_map`）
- Test: `tests/exchanges/test_hl_account_batch.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 5 个方法签名（覆写同签名）。
- Produces: `CcxtAdapter._to_trade(r) -> Trade`；HL `_coin_map() -> Dict[coin, canonical]`。

- [ ] **Step 1: 写失败测试**（stub client；重点钉死 coin 映射，防 funding 同款"查询 symbol 盖到每行"坑）

```python
# tests/exchanges/test_hl_account_batch.py
"""HL 账户级批量读：fills/orders/positions 走 symbol=None，allMids 直调，funding 按 delta.coin 分组。"""
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter

BTC = 'BTC/USDC:USDC'
KPEPE = 'KPEPE/USDC:USDC'

_MARKETS = {
    'BTC/USDC:USDC': {'symbol': 'BTC/USDC:USDC', 'swap': True, 'base': 'BTC',
                      'info': {'name': 'BTC'}},
    'KPEPE/USDC:USDC': {'symbol': 'KPEPE/USDC:USDC', 'swap': True, 'base': 'KPEPE',
                        'info': {'name': 'kPEPE'}},   # HL 原生 coin 名小写 k 前缀
}


class _Client:
    markets = _MARKETS
    def load_markets(self): return self.markets
    def __init__(self):
        self.calls = []
    def fetch_my_trades(self, symbol, since=None):
        self.calls.append(('fetch_my_trades', symbol, since))
        return [{'id': 't1', 'symbol': KPEPE, 'side': 'buy', 'price': 0.009,
                 'amount': 100.0, 'timestamp': 2000, 'order': 'o1',
                 'fee': {'cost': 0.01}, 'info': {}},
                {'id': 't2', 'symbol': BTC, 'side': 'sell', 'price': 50000.0,
                 'amount': 0.1, 'timestamp': 1000, 'order': 'o2',
                 'fee': {'cost': 0.02}, 'info': {}}]
    def fetch_open_orders(self, symbol=None):
        self.calls.append(('fetch_open_orders', symbol))
        return [{'id': 'o3', 'symbol': BTC, 'side': 'buy', 'price': 49000.0,
                 'amount': 0.1, 'filled': 0.0, 'status': 'open', 'info': {}}]
    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(('fetch_positions', symbols))
        return [{'symbol': KPEPE, 'contracts': 12064.0, 'side': 'short',
                 'entryPrice': 0.0095}]
    def publicPostInfo(self, params):
        self.calls.append(('publicPostInfo', params))
        return {'BTC': '50000.5', 'kPEPE': '0.0091', 'ETH': '3000.0'}
    def fetch_funding_history(self, symbol=None, since=None, limit=None):
        self.calls.append(('fetch_funding_history', symbol, since))
        # HL 实况：账户级全币种 + 查询 symbol 盖印到每行 symbol 字段
        return [{'timestamp': 3000, 'amount': -0.5, 'symbol': symbol,
                 'info': {'delta': {'coin': 'kPEPE'}}},
                {'timestamp': 2500, 'amount': 0.2, 'symbol': symbol,
                 'info': {'delta': {'coin': 'BTC'}}},
                {'timestamp': 100, 'amount': -9.9, 'symbol': symbol,
                 'info': {'delta': {'coin': 'BTC'}}}]     # since 之前 → 应被滤掉


def _ad():
    return HyperliquidAdapter(_Client())


def test_trades_all_symbol_none_and_per_row_mapping():
    ad = _ad()
    out = ad.fetch_my_trades_all([BTC, KPEPE], since_ms=500)
    assert ('fetch_my_trades', None, 500) in ad.client.calls   # 账户级：symbol=None
    assert [t.ts for t in out] == [1000, 2000]                 # 升序
    assert {t.symbol for t in out} == {BTC, KPEPE}             # 逐行真实 symbol


def test_trades_all_filters_unwanted_symbols():
    out = _ad().fetch_my_trades_all([BTC], since_ms=None)
    assert [t.symbol for t in out] == [BTC]                    # KPEPE 行被过滤


def test_open_orders_all_and_positions_all():
    ad = _ad()
    orders = ad.fetch_open_orders_all([BTC, KPEPE])
    assert ('fetch_open_orders', None) in ad.client.calls
    assert [o.id for o in orders] == ['o3']
    pos = ad.fetch_positions_all([BTC, KPEPE])
    assert ('fetch_positions', None) in ad.client.calls
    assert pos == {KPEPE: -12064.0}                            # short → 负；BTC 无仓位行


def test_prices_all_via_allmids_with_coin_mapping():
    out = _ad().fetch_prices_all([BTC, KPEPE])
    assert out == {BTC: 50000.5, KPEPE: 0.0091}                # kPEPE→KPEPE 映射；ETH 不在册被滤


def test_funding_all_grouped_by_delta_coin_pay_positive():
    out = _ad().fetch_funding_payments_all([BTC, KPEPE], since_ms=500)
    assert [p.amount for p in out[KPEPE]] == [0.5]             # 支付为正
    assert [(p.ts, p.amount) for p in out[BTC]] == [(2500, -0.2)]   # ts<since 滤掉
```

- [ ] **Step 2: 确认失败**：`.venv/bin/python -m pytest tests/exchanges/test_hl_account_batch.py -q` → FAIL
- [ ] **Step 3: 实现**

ccxt_adapter.py——把 `fetch_my_trades` 的行解析抽成 `_to_trade`（放 `_to_order` 旁）：

```python
    def _to_trade(self, r) -> Trade:
        return Trade(
            id=str(r['id']),
            client_oid=str((r.get('info', {}) or {}).get('clOrdId') or r.get('order') or r['id']),
            symbol=self.to_canonical(r['symbol']), side=r['side'],
            price=float(r['price']), size=float(r['amount']),
            fee=float((r.get('fee') or {}).get('cost') or 0.0), ts=int(r['timestamp']),
            order_id=(str(r['order']) if r.get('order') is not None else None))
```

`fetch_my_trades` 循环体改为 `out.append(self._to_trade(r))`（行为不变）。

hyperliquid.py 追加：

```python
    # ---- 账户级批量读（HL 原生：fills/orders/positions/funding 端点本就账户级）----
    def _coin_map(self):
        # HL 原生 coin 名（如 'kPEPE'）→ canonical symbol。必须经 ccxt markets 映射，
        # 勿 f-string 拼接（大小写/前缀会错）。实例内缓存（新上币重启进程后可见）。
        if getattr(self, '_coin_map_cache', None) is None:
            self.client.load_markets()
            m2 = {}
            for m in self.client.markets.values():
                if m.get('swap') is not True:
                    continue
                coin = ((m.get('info') or {}).get('name')) or m.get('base')
                m2[coin] = self.to_canonical(m['symbol'])
            self._coin_map_cache = m2
        return self._coin_map_cache

    def fetch_my_trades_all(self, symbols, since_ms=None):
        want = set(symbols)
        out = [self._to_trade(r) for r in self.client.fetch_my_trades(None, since=since_ms)]
        out = [t for t in out if t.symbol in want]
        out.sort(key=lambda t: t.ts)
        return out

    def fetch_open_orders_all(self, symbols):
        want = set(symbols)
        return [o for o in (self._to_order(r) for r in self.client.fetch_open_orders(None))
                if o.symbol in want]

    def fetch_positions_all(self, symbols):
        want = set(symbols)
        out = {}
        for p in self.client.fetch_positions():
            sym = self.to_canonical(p['symbol'])
            if sym not in want:
                continue
            contracts = float(p.get('contracts') or 0.0)
            out[sym] = contracts if p.get('side') == 'long' else -contracts
        return out

    def fetch_prices_all(self, symbols):
        # allMids 权重 2（fetchTickers 走高权重端点，不用）
        mids = self.client.publicPostInfo({'type': 'allMids'}) or {}
        cmap = self._coin_map()
        want = set(symbols)
        return {cmap[c]: float(px) for c, px in mids.items()
                if cmap.get(c) in want}

    def fetch_funding_payments_all(self, symbols, since_ms=None):
        # userFunding 本就账户级且把查询 symbol 盖到每行（见 fetch_funding_payments 注释）；
        # 任取一个 symbol 触发查询，按 info.delta.coin 分组回各币种。
        probe = symbols[0] if symbols else None
        rows = self.client.fetch_funding_history(
            self.to_native(probe) if probe else None, since=since_ms)
        cmap = self._coin_map()
        out = {s: [] for s in symbols}
        for r in rows:
            ts = int(r['timestamp'])
            if since_ms is not None and ts < since_ms:
                continue
            coin = ((r.get('info') or {}).get('delta') or {}).get('coin')
            sym = cmap.get(coin)
            if sym not in out:
                continue
            out[sym].append(FundingPayment(ts=ts, amount=-float(r['amount'])))
        for s in out:
            out[s].sort(key=lambda p: p.ts)
        return out
```

- [ ] **Step 4: 确认通过** + `tests/exchanges/ -q` 全绿（`_to_trade` 重构不改行为）
- [ ] **Step 5: Commit** `feat(exchanges): HL 原生账户级批量读（fills/orders/positions/allMids/funding按coin分组）`

---

### Task 3: ResilientAdapter 包装 + 电路归类

**Files:**
- Modify: `gridtrade/exchanges/resilient_adapter.py`
- Test: `tests/exchanges/test_resilient_adapter.py`（追加）

**Interfaces:**
- Consumes: Task 1 签名。
- Produces: ResilientAdapter 透传 5 方法；`fetch_prices_all`→market_read，其余 4 个→account_read。

- [ ] **Step 1: 追加失败测试**

```python
def test_account_batch_methods_wrapped_with_categories():
    # _all 读方法走电路：4 个账户读共 account_read 一路，prices_all 归 market_read。
    from gridtrade.exchanges.resilience import CircuitOpenError
    from gridtrade.exchanges.resilient_adapter import default_breakers

    class _Inner3(_Inner):
        def fetch_my_trades_all(self, symbols, since_ms=None):
            self.calls.append(('fetch_my_trades_all', tuple(symbols)))
            self._maybe_fail('fetch_my_trades_all')
            return []
        def fetch_positions_all(self, symbols):
            self.calls.append(('fetch_positions_all', tuple(symbols)))
            return {}
        def fetch_prices_all(self, symbols):
            self.calls.append(('fetch_prices_all', tuple(symbols)))
            return {s: 1.0 for s in symbols}

    brs = {k: CircuitBreaker(failure_threshold=2, cooldown=999.0, clock=lambda: 0.0)
           for k in default_breakers()}
    inner = _Inner3().fail('fetch_my_trades_all', 99, ccxt.NetworkError('down'))
    ra = _resilient(inner, policy=RetryPolicy(max_attempts=1), breakers=brs)
    for _ in range(2):
        with pytest.raises(ccxt.NetworkError):
            ra.fetch_my_trades_all(['X'])
    with pytest.raises(CircuitOpenError):
        ra.fetch_positions_all(['X'])          # 同 account_read 路被熔断
    assert ra.fetch_prices_all(['X']) == {'X': 1.0}   # market_read 不受影响
```

- [ ] **Step 2: 确认失败**（ResilientAdapter 无 `_all` 方法 → AttributeError/未走电路）
- [ ] **Step 3: 实现**：`ACCOUNT_READ_METHODS` 增补 `'fetch_my_trades_all', 'fetch_open_orders_all', 'fetch_positions_all', 'fetch_funding_payments_all'`（`fetch_prices_all` 不列 = 默认 market_read）；类内追加：

```python
    # ---- 账户级批量读（monitor 快照）----
    def fetch_my_trades_all(self, symbols, since_ms=None):
        return self._call('fetch_my_trades_all', symbols, since_ms=since_ms)

    def fetch_open_orders_all(self, symbols):
        return self._call('fetch_open_orders_all', symbols)

    def fetch_positions_all(self, symbols):
        return self._call('fetch_positions_all', symbols)

    def fetch_prices_all(self, symbols):
        return self._call('fetch_prices_all', symbols)

    def fetch_funding_payments_all(self, symbols, since_ms=None):
        return self._call('fetch_funding_payments_all', symbols, since_ms=since_ms)
```

- [ ] **Step 4: 确认通过** + 本文件全绿
- [ ] **Step 5: Commit** `feat(exchanges): ResilientAdapter 包装账户级批量读（prices→market_read，余→account_read）`

---

### Task 4: snapshot.py（AccountSnapshot + 构建器）

**Files:**
- Create: `gridtrade/execution/snapshot.py`
- Test: `tests/execution/test_snapshot.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 5 个 adapter 方法。
- Produces:
  - `AccountSnapshot`：字段 `ts_ms/trades/orders_by_symbol/positions/prices/funding_by_symbol`；
    方法 `trades_for(symbol, since_ms=0)`、`orders_for(symbol)`、`position(symbol)->Optional[float]`、
    `price(symbol)->Optional[float]`、`funding_for(symbol, since_ms=0)`
  - `build_account_snapshot(adapter, symbols, *, trade_since_ms=0, funding_since_ms=0) -> AccountSnapshot`（任一调用失败异常上抛）

- [ ] **Step 1: 写失败测试**

```python
# tests/execution/test_snapshot.py
"""AccountSnapshot：视图过滤/构建/失败传播。数据源用 FakeExchange（base 默认 _all）。"""
import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.snapshot import AccountSnapshot, build_account_snapshot

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'


def _fake():
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=100.0)
    ex.set_price(BTC, 100.0); ex.set_price(ETH, 200.0)
    ex.create_limit_order(BTC, 'buy', 99.0, 1.0, client_oid='b1')
    ex.create_limit_order(ETH, 'sell', 201.0, 2.0, client_oid='e1')
    ex.set_price(BTC, 98.5)                     # BTC 买单成交
    return ex


def test_build_and_views():
    ex = _fake()
    snap = build_account_snapshot(ex, [BTC, ETH])
    assert snap.trades_for(BTC) == ex.fetch_my_trades(BTC)
    assert snap.trades_for(ETH) == []
    assert [o.id for o in snap.orders_for(ETH)] == [o.id for o in ex.fetch_open_orders(ETH)]
    assert snap.position(BTC) == ex.fetch_positions(BTC).net_size
    assert snap.price(ETH) == 200.0
    assert snap.price('NOPE/USDT:USDT') is None     # 缺币价 → None（调用方降级）
    assert snap.funding_for(BTC) == ex.fetch_funding_payments(BTC)


def test_trades_for_since_filter():
    ex = _fake()
    snap = build_account_snapshot(ex, [BTC])
    ts = snap.trades_for(BTC)[0].ts
    assert snap.trades_for(BTC, since_ms=ts) != []      # 含边界（>=）
    assert snap.trades_for(BTC, since_ms=ts + 1) == []


def test_build_failure_propagates():
    ex = _fake()
    def boom(symbols, since_ms=None):
        raise RuntimeError('endpoint down')
    ex.fetch_my_trades_all = boom
    with pytest.raises(RuntimeError):
        build_account_snapshot(ex, [BTC])
```

- [ ] **Step 2: 确认失败**（模块不存在）
- [ ] **Step 3: 实现**

```python
# gridtrade/execution/snapshot.py
"""AccountSnapshot：monitor 轮首账户级批量读（设计：docs/superpowers/specs/2026-07-06-account-snapshot-batch-reads-design.md）。

每轮 5 次账户级调用替代逐格逐调（HL 全部为账户级端点，权重与格数解耦）。
不可变只读对象，并行单元共享零竞态；构建失败异常上抛，由 cycle 整轮跳过。
"""
from dataclasses import dataclass

from gridtrade.state.models import now_ms


@dataclass(frozen=True)
class AccountSnapshot:
    ts_ms: int
    trades: tuple              # Trade 升序
    orders_by_symbol: dict     # {symbol: tuple(Order)}
    positions: dict            # {symbol: net_size 带符号}
    prices: dict               # {symbol: mid}
    funding_by_symbol: dict    # {symbol: tuple(FundingPayment)} 升序

    def trades_for(self, symbol, since_ms=0):
        return [t for t in self.trades if t.symbol == symbol and t.ts >= since_ms]

    def orders_for(self, symbol):
        return list(self.orders_by_symbol.get(symbol, ()))

    def position(self, symbol):
        return self.positions.get(symbol)      # None=快照无此仓位行（调用方视为 0）

    def price(self, symbol):
        return self.prices.get(symbol)         # None=缺币价（调用方降级报错）

    def funding_for(self, symbol, since_ms=0):
        return [p for p in self.funding_by_symbol.get(symbol, ()) if p.ts >= since_ms]


def build_account_snapshot(adapter, symbols, *, trade_since_ms=0,
                           funding_since_ms=0) -> AccountSnapshot:
    """5 次账户级调用（经 ResilientAdapter 电路）。任一失败 → 异常上抛。"""
    symbols = sorted(set(symbols))
    trades = sorted(adapter.fetch_my_trades_all(symbols, since_ms=trade_since_ms),
                    key=lambda t: t.ts)
    by_sym = {}
    for o in adapter.fetch_open_orders_all(symbols):
        by_sym.setdefault(o.symbol, []).append(o)
    positions = dict(adapter.fetch_positions_all(symbols))
    prices = dict(adapter.fetch_prices_all(symbols))
    funding = {s: tuple(v) for s, v in
               adapter.fetch_funding_payments_all(symbols, since_ms=funding_since_ms).items()}
    return AccountSnapshot(ts_ms=now_ms(), trades=tuple(trades),
                           orders_by_symbol={s: tuple(v) for s, v in by_sym.items()},
                           positions=positions, prices=prices,
                           funding_by_symbol=funding)
```

- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** `feat(execution): AccountSnapshot 快照对象与构建器`

---

### Task 5: GridExecutor.sync + monitor_grid 快照参数

**Files:**
- Modify: `gridtrade/execution/grid_executor.py:138-218`（sync）
- Modify: `gridtrade/execution/monitor.py`（monitor_grid 透传）
- Test: `tests/execution/test_sync_snapshot.py`（新建）

**Interfaces:**
- Consumes: Task 4 `AccountSnapshot` 视图方法。
- Produces: `sync(grid_id, symbol, *, skip_replenish=False, snapshot=None)`；
  `monitor_grid(executor, grid_id, symbol, stop_cfg, *, margin_rate=0.05, skip_replenish=False, pv_spike=0, funding_rate=0.0, snapshot=None)`。

- [ ] **Step 1: 写失败测试**（双路径差分等价 + 缺币价降级）

```python
# tests/execution/test_sync_snapshot.py
"""sync 快照供给 vs 逐格取数 双路径终态等价（成交摄入/标closed/补单/记账）。"""
import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.snapshot import build_account_snapshot
from gridtrade.state.store import StateStore

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    gid = gx.open('fake', BTC, dict(GP), tag='t0')
    return ex, gx, gid


def _end_state(gx, gid):
    acc = gx.accounting.get(gid)
    return (sorted(f.trade_id for f in gx.fills.list_by_grid(gid)),
            sorted((o.line_index, o.side, o.status) for o in gx.orders.list_by_grid(gid)),
            round(acc.net_position, 9), round(acc.realized_pnl, 9), round(acc.fee_paid, 9))


def test_sync_with_snapshot_equals_plain_sync(store):
    ex1, gx1, g1 = _setup(store)
    st2 = StateStore.in_memory(); st2.create_all()
    try:
        ex2, gx2, g2 = _setup(st2)
        ex1.set_price(BTC, 100.6)          # 同样的卖单成交
        ex2.set_price(BTC, 100.6)
        r1 = gx1.sync(g1, BTC)             # 旧路径
        snap = build_account_snapshot(ex2, [BTC])
        r2 = gx2.sync(g2, BTC, snapshot=snap)   # 快照路径
        assert r1['new_fills'] == r2['new_fills'] == 1
        s1, s2 = _end_state(gx1, g1), _end_state(gx2, g2)
        assert s1[1:] == s2[1:]            # 订单/仓位/盈亏/费用逐项等价（trade_id 因独立所生成不同）
        assert len(s1[0]) == len(s2[0]) == 1
    finally:
        st2.dispose_and_cleanup()


def test_sync_snapshot_missing_price_raises(store):
    ex, gx, gid = _setup(store)
    snap = build_account_snapshot(ex, [])   # 空 symbols → 无 BTC 价格
    with pytest.raises(RuntimeError):
        gx.sync(gid, BTC, snapshot=snap)


def test_sync_snapshot_respects_grid_cursor(store):
    # 快照含全账户成交，本格仍按自己游标过滤：已摄入的不重复（add_if_new 幂等兜底之前先被 since 滤掉）
    ex, gx, gid = _setup(store)
    ex.set_price(BTC, 100.6)
    gx.sync(gid, BTC)                       # 先旧路径摄入
    snap = build_account_snapshot(ex, [BTC])
    r = gx.sync(gid, BTC, snapshot=snap)    # 再快照路径跑一轮
    assert r['new_fills'] == 0              # 不重复摄入
```

- [ ] **Step 2: 确认失败**（sync 无 snapshot 参数 → TypeError）
- [ ] **Step 3: 实现**

grid_executor.py `sync` 签名与三处取数改造（其余逻辑一行不动）：

```python
    def sync(self, grid_id, symbol, *, skip_replenish=False, snapshot=None):
        geom = self._geom[grid_id]
        price_array = geom['price_array']
        order_num = geom['order_num']
        cursor = max(0, self.fills.max_ts(grid_id) - _TRADE_REFETCH_OVERLAP_MS)
        # 快照=轮首账户级批量读（权重与格数解耦）；None=逐格取数（测试基线/回退面）
        trades = (snapshot.trades_for(symbol, since_ms=cursor) if snapshot is not None
                  else self.adapter.fetch_my_trades(symbol, since_ms=cursor))
```

（原 `trades = self.adapter.fetch_my_trades(...)` 行替换为上两行；后续 `candidates` 起全部不动。）

资金费段：

```python
        fcur = self._funding_cursor.get(grid_id, 0)
        pays = (snapshot.funding_for(symbol, since_ms=fcur) if snapshot is not None
                else self.adapter.fetch_funding_payments(symbol, since_ms=fcur))
```

现价段：

```python
        if snapshot is not None:
            px = snapshot.price(symbol)
            if px is None:      # 快照缺币价（allMids 罕见缺行）→ 本格降级，勿用 0 价算净值
                raise RuntimeError('snapshot missing price for %s' % symbol)
        else:
            px = self.adapter.fetch_price(symbol)
        snap = self.live[grid_id].snapshot(float(px))
```

monitor.py：

```python
def monitor_grid(executor, grid_id, symbol, stop_cfg, *, margin_rate=0.05, skip_replenish=False,
                 pv_spike=0, funding_rate=0.0, snapshot=None):
    res = executor.sync(grid_id, symbol, skip_replenish=skip_replenish, snapshot=snapshot)
```

（其余不动。）

- [ ] **Step 4: 确认通过** + `tests/execution/ -q` 全绿
- [ ] **Step 5: Commit** `feat(execution): sync/monitor_grid 支持 AccountSnapshot 供给（None=旧路径）`

---

### Task 6: Reconciler 三方法快照参数

**Files:**
- Modify: `gridtrade/execution/reconciler.py`（reconcile_open_orders / check_position_drift / _fuse_filled / reconcile_fuses）
- Test: `tests/execution/test_reconciler_snapshot.py`（新建）

**Interfaces:**
- Consumes: Task 4 视图方法。
- Produces: `reconcile_open_orders(grid_id, symbol, snapshot=None)`、
  `check_position_drift(grid_id, symbol, *, tol_lots=1.5, snapshot=None)`、
  `reconcile_fuses(grid_id, symbol, snapshot=None)`。

- [ ] **Step 1: 写失败测试**

```python
# tests/execution/test_reconciler_snapshot.py
"""reconcile 三方法快照供给等价：对账/漂移/保险丝三态。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.snapshot import build_account_snapshot

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, stop_orders=False):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, stop_orders_enabled=stop_orders)
    gid = gx.open('fake', BTC, dict(GP), tag='t0')
    return ex, gx, gid


def test_reconcile_open_orders_snapshot_clean(store):
    ex, gx, gid = _setup(store)
    rec = Reconciler(gx)
    snap = build_account_snapshot(ex, [BTC])
    assert rec.reconcile_open_orders(gid, BTC, snapshot=snap) == {'canceled': 0, 'replaced': 0}


def test_reconcile_snapshot_grace_then_replace(store):
    # 挂单从快照消失（且成交不可见）→ 宽限 2 轮后重挂，与逐格路径同语义
    ex, gx, gid = _setup(store)
    rec = Reconciler(gx)                       # replace_grace=2
    sell = [o for o in ex.fetch_open_orders(BTC) if o.side == 'sell'][0]
    ex._open.pop(sell.id, None)                # 从交易所丢单（成交不可见）
    snap = build_account_snapshot(ex, [BTC])
    assert rec.reconcile_open_orders(gid, BTC, snapshot=snap)['replaced'] == 0   # 第 1 轮宽限
    snap = build_account_snapshot(ex, [BTC])
    assert rec.reconcile_open_orders(gid, BTC, snapshot=snap)['replaced'] == 1   # 第 2 轮重挂


def test_position_drift_via_snapshot(store):
    ex, gx, gid = _setup(store)
    gx.sync(gid, BTC)
    rec = Reconciler(gx)
    ex.create_market_order(BTC, 'sell', 3 * gx._geom[gid]['order_num'],
                           client_oid='external:0')     # 外部动仓
    snap = build_account_snapshot(ex, [BTC])
    d = rec.check_position_drift(gid, BTC, snapshot=snap)
    assert d is not None and d['ok'] is False


def test_position_drift_snapshot_missing_position_means_zero(store):
    ex, gx, gid = _setup(store)
    rec = Reconciler(gx)
    snap = build_account_snapshot(ex, [BTC])
    d = rec.check_position_drift(gid, BTC, snapshot=snap)
    assert d is not None and d['exchange'] == 0.0       # 无仓位行 → 0（开网即 flat）


def test_fuse_replaced_and_fired_via_snapshot(store):
    ex, gx, gid = _setup(store, stop_orders=True)
    rec = Reconciler(gx)
    g = gx.grids.get(gid)
    # 保险丝在挂 → 无动作
    snap = build_account_snapshot(ex, [BTC])
    assert rec.reconcile_fuses(gid, BTC, snapshot=snap) == {'replaced': 0, 'fired': False}
    # 丢一根（低侧）且未成交 → 重挂
    ex._open.pop(g.fuse_low_oid, None)
    snap = build_account_snapshot(ex, [BTC])
    out = rec.reconcile_fuses(gid, BTC, snapshot=snap)
    assert out == {'replaced': 1, 'fired': False}
```

- [ ] **Step 2: 确认失败**（TypeError: snapshot 参数不存在）
- [ ] **Step 3: 实现**（reconciler.py 四处，逻辑其余不动）

```python
    def reconcile_open_orders(self, grid_id, symbol, snapshot=None):
        ex = self.ex
        expected = {o.exchange_order_id: o for o in ex.orders.list_open_by_grid(grid_id)
                    if o.exchange_order_id}
        src = (snapshot.orders_for(symbol) if snapshot is not None
               else ex.adapter.fetch_open_orders(symbol))
        on_exchange = {o.id: o for o in src}
```

```python
    def check_position_drift(self, grid_id, symbol, *, tol_lots=1.5, snapshot=None):
        ...
        if snapshot is not None:
            pos = snapshot.position(symbol)
            real = float(pos) if pos is not None else 0.0   # 快照无仓位行 = 交易所 flat
        else:
            real = float(ex.adapter.fetch_positions(symbol).net_size)
```

```python
    def _fuse_filled(self, symbol, oid, since_ms=None, snapshot=None):
        if oid is None:
            return False
        trades = (snapshot.trades_for(symbol) if snapshot is not None
                  else self.ex.adapter.fetch_my_trades(symbol, since_ms=since_ms))
        return any(t.order_id == oid for t in trades)

    def reconcile_fuses(self, grid_id, symbol, snapshot=None):
        ...
        src = (snapshot.orders_for(symbol) if snapshot is not None
               else ex.adapter.fetch_open_orders(symbol))
        on_exchange = {o.id for o in src}
        ...
            if self._fuse_filled(symbol, oid, snapshot=snapshot):
```

- [ ] **Step 4: 确认通过** + `tests/execution/ -q` 全绿
- [ ] **Step 5: Commit** `feat(execution): Reconciler 对账/漂移/保险丝支持快照供给`

---

### Task 7: cycles 接线（轮首构建/透传/失败整轮跳过）+ 旧测试校准

**Files:**
- Modify: `gridtrade/runtime/cycles.py`（`_grid_unit` 加 snapshot 透传；`run_monitor_cycle` 轮首构建）
- Modify: `tests/runtime/test_monitor_cycle_control.py`（halt 桩补 fills/accounting/adapter）
- Modify: `tests/runtime/test_chaos_cycle.py`（读故障语义校准：单 symbol 读故障→整轮跳过，如有此形态）
- Test: `tests/runtime/test_cycles_snapshot.py`（新建）

**Interfaces:**
- Consumes: Task 4 `build_account_snapshot`、Task 5/6 的 snapshot 参数。
- Produces: cycle 内部行为（对外返回 dict 契约不变）。

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_cycles_snapshot.py
"""cycle 快照接线：单元零逐格读、失败整轮跳过、E2 宽限×快照时序不幻影重挂。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.gates import GridProposal, GateChain, SymbolLockGate
from gridtrade.execution.manager import GridManager
from gridtrade.runtime.cycles import run_monitor_cycle

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}

PER_SYMBOL_READS = ('fetch_my_trades', 'fetch_open_orders', 'fetch_positions',
                    'fetch_price', 'fetch_funding_payments')


class _Counting:
    """委托 FakeExchange；计数逐 symbol 读调用。_all 走 inner 的 base 默认实现
    （inner 内部自调不经本包装，故计数只反映单元直发的逐格读——应为 0）。"""
    def __init__(self, inner):
        self._inner = inner
        self.reads = {m: 0 for m in PER_SYMBOL_READS}
    def __getattr__(self, name):
        return getattr(self._inner, name)
    def fetch_my_trades(self, symbol, since_ms=None):
        self.reads['fetch_my_trades'] += 1
        return self._inner.fetch_my_trades(symbol, since_ms=since_ms)
    def fetch_open_orders(self, symbol):
        self.reads['fetch_open_orders'] += 1
        return self._inner.fetch_open_orders(symbol)
    def fetch_positions(self, symbol):
        self.reads['fetch_positions'] += 1
        return self._inner.fetch_positions(symbol)
    def fetch_price(self, symbol):
        self.reads['fetch_price'] += 1
        return self._inner.fetch_price(symbol)
    def fetch_funding_payments(self, symbol, since_ms=None):
        self.reads['fetch_funding_payments'] += 1
        return self._inner.fetch_funding_payments(symbol, since_ms=since_ms)


def _setup(store):
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=100.0)
    ex.set_price(BTC, 100.0); ex.set_price(ETH, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    mgr = GridManager(gx, GateChain([SymbolLockGate(gx.grids)]), stop_cfg=STOP_CFG)
    mgr.open_proposals([
        GridProposal(exchange='fake', symbol=BTC, grid_params=dict(GP), offset=0, tag='t0', source='t'),
        GridProposal(exchange='fake', symbol=ETH, grid_params=dict(GP), offset=0, tag='t1', source='t')])
    return ex, gx, mgr


def test_units_do_zero_per_symbol_reads(store):
    ex, gx, mgr = _setup(store)
    wrapped = _Counting(ex)
    gx.adapter = wrapped
    out = run_monitor_cycle(Reconciler(gx), mgr, parallel=4)
    assert len(out['monitored']) == 2 and out['degraded'] == {}
    assert wrapped.reads == {m: 0 for m in PER_SYMBOL_READS}   # 全部读来自快照


def test_snapshot_failure_skips_round_gracefully(store):
    ex, gx, mgr = _setup(store)
    class _Broken(_Counting):
        def fetch_my_trades_all(self, symbols, since_ms=None):
            raise RuntimeError('batch endpoint down')
    gx.adapter = _Broken(ex)
    logs, beats = [], []
    out = run_monitor_cycle(Reconciler(gx), mgr, parallel=4, log=logs.append,
                            beat=lambda: beats.append(1), beat_every_sec=0.0)
    assert out['monitored'] == [] and out['reconciled'] == {}   # 整轮跳过
    assert any('snapshot failed' in s for s in logs)
    assert beats                                                # 心跳照打


def test_fill_replenish_not_phantom_replaced_across_rounds(store):
    # E2 宽限 × 快照时序：本轮补挂的新单在轮首快照缺席（missing 计 1），
    # 下轮新快照可见（清零）→ 两轮 replaced 均为 0、不产生重复挂单。
    # 反例警示：replace_grace=1 时本语义会被破坏（新单当轮即被误重挂）。
    ex, gx, mgr = _setup(store)
    rec = Reconciler(gx)
    gid = [g.id for g in gx.grids.list_active() if g.symbol == BTC][0]
    ex.set_price(BTC, 100.6)                     # 卖单成交 → sync 补对侧
    out1 = run_monitor_cycle(rec, mgr)
    assert out1['reconciled'][gid]['replaced'] == 0
    out2 = run_monitor_cycle(rec, mgr)
    assert out2['reconciled'][gid]['replaced'] == 0
    lines = [(o.line_index, o.side) for o in gx.orders.list_by_grid(gid)
             if o.status == 'open']
    assert len(lines) == len(set(lines))         # 无重复挂单
```

- [ ] **Step 2: 确认失败**：`test_units_do_zero_per_symbol_reads` 读计数非零（现状逐格取数）
- [ ] **Step 3: 实现**（cycles.py）

`_grid_unit` 签名加 `snapshot=None`，两处透传：

```python
def _grid_unit(reconciler, manager, grid, *, skip_replenish=False, snapshot=None) -> dict:
    ...
        res = monitor_grid(ex, grid.id, grid.symbol, manager.stop_cfg,
                           margin_rate=manager.margin_rate,
                           skip_replenish=skip_replenish,
                           pv_spike=pv_spike, funding_rate=funding_rate,
                           snapshot=snapshot)
        ...
            out['reconciled'] = reconciler.reconcile_open_orders(grid.id, grid.symbol,
                                                                 snapshot=snapshot)
            d = reconciler.check_position_drift(grid.id, grid.symbol, snapshot=snapshot)
            ...
            out['fuse'] = reconciler.reconcile_fuses(grid.id, grid.symbol, snapshot=snapshot)
```

`run_monitor_cycle`：`halted = ...` 之后、派发之前插入（并给两条派发路径传 `snapshot=snapshot`）：

```python
    from gridtrade.execution.grid_executor import _TRADE_REFETCH_OVERLAP_MS
    from gridtrade.execution.snapshot import build_account_snapshot

    active = _active_grids(ex.grids)
    snapshot = None
    snap_failed = False
    if active:
        try:
            # 游标口径：无成交的新格用 created_at（修掉旧的 cursor=0 全量扫）；
            # funding 游标读 DB（单元里的惰性 restore 尚未发生，不能依赖内存态）。
            t_base, f_base = [], []
            for g in active:
                last = ex.fills.max_ts(g.id)
                t_base.append(int(last) if last else int(g.created_at))
                acc = ex.accounting.get(g.id)
                f_base.append(int(acc.funding_cursor) if (acc is not None and acc.funding_cursor)
                              else int(g.created_at))
            snapshot = build_account_snapshot(
                ex.adapter, sorted({g.symbol for g in active}),
                trade_since_ms=max(0, min(t_base) - _TRADE_REFETCH_OVERLAP_MS),
                funding_since_ms=max(0, min(f_base)))
        except Exception as exc:      # 整轮跳过（用户决定）：下轮重建；保险丝在交易所侧独立护网
            snap_failed = True
            log('[monitor] snapshot failed: %r (units skipped this round)' % exc)

    results: List[dict] = []
    if snap_failed:
        pass                          # 不派发任何单元；指令/equity/心跳照常
    elif parallel <= 1 or len(active) <= 1:
        for grid in active:
            results.append(_grid_unit(reconciler, manager, grid,
                                      skip_replenish=halted, snapshot=snapshot))
            _maybe_beat()
    else:
        ...（线程池路径同样 pool.submit(_grid_unit, ..., skip_replenish=halted, snapshot=snapshot)）
```

（import 放模块顶：`from gridtrade.execution.grid_executor import _TRADE_REFETCH_OVERLAP_MS`、
`from gridtrade.execution.snapshot import build_account_snapshot`，不放函数内。）

- [ ] **Step 4: 校准两处旧测试**
  - `test_monitor_cycle_control.py`：`_Executor` 桩补 `fills`（`max_ts(gid)->0`）、`accounting`（`get(gid)->None`）、`adapter=FakeExchange(instruments=[], price=1.0)`；`_Grid` 桩 `created_at=0` 已有。首测（零活跃格）不受影响；halt 测继续断言 `seen['skip'] is True`。
  - `test_chaos_cycle.py`：读故障场景若断言"单格降级、其余照常"，改为断言"快照失败整轮跳过 + 日志 + 下轮（故障恢复后）全格恢复"，并注释：账户级快照下读故障 blast radius=一轮（HL 原生实现单端点本就原子）；写故障场景断言不变（写仍逐格）。
- [ ] **Step 5: 全绿确认**：`tests/runtime/ -q`，然后全套 `.venv/bin/python -m pytest -q`
- [ ] **Step 6: Commit** `feat(monitor): 轮首 AccountSnapshot 接线（单元零逐格读；失败整轮跳过）`

---

### Task 8: 收尾——全套回归 + 文档

**Files:**
- Modify: `docs/STATUS.md`（若含 monitor 权重/取数描述则同步一行）
- Test: 全套

- [ ] **Step 1:** `.venv/bin/python -m pytest -q` 全绿（预期 ~600）
- [ ] **Step 2:** `git log --oneline` 核对 Task 1-7 各自成 commit
- [ ] **Step 3:** STATUS.md 部署段补一行：monitor 读路径已快照化（每轮 5 次账户级调用），部署前须完成 spec「上线前硬性验证项」（真 testnet 直调 fetchMyTrades(None)/fetchOpenOrders(None)/allMids）
- [ ] **Step 4: Commit** `docs(status): monitor 快照化取数落地记录`

---

## Self-Review 结果

- **Spec 覆盖**：§1 快照对象→Task 4；§2 五方法（base 默认/HL 原生/Resilient 包装）→Task 1/2/3；§3 sync/reconciler→Task 5/6；§4 语义边界（宽限交互/失败语义）→Task 7 测试；§5 cycle 接线→Task 7；游标口径→Task 7 Step 3；测试策略各条均有对应；上线前硬性验证项→部署阶段（Task 8 记入 STATUS）。FakeExchange 原生 `_all`：spec 提及，但 base 默认实现对 Fake 已零阻力且行为等价——按 YAGNI 不做覆写（差分测试反而依赖默认实现，覆写会让 Task 1 测试失去意义）。此为对 spec 的一处有意收窄。
- **占位符**：无 TBD/TODO；所有代码步骤给出完整代码。
- **类型一致性**：`_all` 五签名在 Task 1 定义、Task 2/3/4 消费一致；`snapshot=None` 参数名全链路一致（monitor_grid→sync、_grid_unit→reconcile_*）。
