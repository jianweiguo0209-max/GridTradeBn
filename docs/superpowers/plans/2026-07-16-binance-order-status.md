# 覆写 BinanceAdapter.order_status(修保险丝触发漏判) 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 覆写 `BinanceAdapter.order_status` 为双簿权威查单,让保险丝三态对账认得出"已触发"→ 摄入丝成交+关格,修账本背离熔断(顺带修 E2 补单三态 + churn)。

**Architecture:** `order_status` 双簿查单(常规 `fetch_order` → `ccxt.OrderNotFound` → `{'trigger':True}` algo/trigger 簿,同既有 `cancel_order` 模式)+ ccxt 状态映射。纯实盘 API,reconciler 三态逻辑已正确(既有测试证),只恢复其 order_status 输入。

**Tech Stack:** Python 3.9 / pytest / ccxt 4.5.61(`fetch_order` + `ccxt.OrderNotFound`)。

## Global Constraints

- **双簿查单**:先 `fetch_order(oid, native, {})`(常规簿);`ccxt.OrderNotFound` → 重试 `fetch_order(oid, native, {'trigger': True})`(algo/trigger 簿)。两簿皆 `OrderNotFound` → 返 `'unknown'`(保留调用方 `_fuse_filled`/sync fills 兜底)。
- **状态映射**:ccxt 归一化 status `'closed'`→`'filled'`;`'open'`(含 NEW/PARTIALLY_FILLED)→`'open'`;`'canceled'`→`'canceled'`;其余/None→`'unknown'`。
- **PARTIALLY_FILLED→'open'**:与 `FakeExchange.order_status` 语义一致(在挂优先于成交,fake.py:193)。
- **不改** reconciler 三态逻辑、`ingest_fuse_fills`、`ResilientAdapter.order_status` 转发(已在 :166)、base 默认。
- 测试命令:`.venv/bin/python -m pytest <path> -q -o addopts=""`(`-o addopts=""` 必带)。提交尾注 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。**不部署**。
- 回测无关(纯实盘 API,FakeExchange.order_status 已三态、不变)。

---

### Task 1: 覆写 `BinanceAdapter.order_status`(双簿 + 状态映射)

**Files:**
- Modify: `gridtrade/exchanges/binance.py`(加 `order_status` + `_map_order_status`,置于 `cancel_order`(:230)之后)
- Test: `tests/exchanges/test_binance_adapter.py`

**Interfaces:**
- Produces: `BinanceAdapter.order_status(symbol, order_id) -> str`('open'/'filled'/'canceled'/'unknown')——覆写 base 默认 'unknown';reconciler 丝三态/E2 补单三态消费(现有调用点,无需改)。

- [ ] **Step 1: 写失败测试**

在 `tests/exchanges/test_binance_adapter.py` 末尾追加:

```python
def test_order_status_regular_book_maps_status():
    import ccxt
    c = FakeBinanceClient()
    calls = []
    def fake_fetch_order(oid, symbol=None, params=None):
        calls.append(dict(params or {}))
        if (params or {}).get('trigger'):
            raise ccxt.OrderNotFound('binanceusdm order not in trigger book')
        return {'status': 'closed'}          # 常规簿命中,FILLED
    c.fetch_order = fake_fetch_order
    assert _binance(c).order_status('BTC/USDT:USDT', '115773892') == 'filled'
    assert calls == [{}]                      # 常规簿命中即返,不试 trigger


def test_order_status_algo_book_falls_back_to_trigger():
    import ccxt
    c = FakeBinanceClient()
    calls = []
    def fake_fetch_order(oid, symbol=None, params=None):
        calls.append(dict(params or {}))
        if not (params or {}).get('trigger'):
            raise ccxt.OrderNotFound('binanceusdm not in regular book')
        return {'status': 'closed'}          # algo/trigger 簿命中(丝触发=FILLED)
    c.fetch_order = fake_fetch_order
    assert _binance(c).order_status('BTC/USDT:USDT', '1000000136629136') == 'filled'
    assert calls == [{}, {'trigger': True}]   # 常规→OrderNotFound→trigger


def test_order_status_maps_all_ccxt_statuses():
    import ccxt
    a = _binance()
    for ccxt_status, expect in (('open', 'open'), ('closed', 'filled'),
                                ('canceled', 'canceled'), (None, 'unknown'),
                                ('weird', 'unknown')):
        assert a._map_order_status(ccxt_status) == expect


def test_order_status_both_books_not_found_returns_unknown():
    import ccxt
    c = FakeBinanceClient()
    def fake_fetch_order(oid, symbol=None, params=None):
        raise ccxt.OrderNotFound('binanceusdm gone from both books')
    c.fetch_order = fake_fetch_order
    assert _binance(c).order_status('BTC/USDT:USDT', '999') == 'unknown'
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q -o addopts="" -k "order_status"`
Expected: FAIL —— `_map_order_status` 不存在 / order_status 返基类 'unknown' 不走 fetch_order(`calls` 断言不符)。

- [ ] **Step 3: 实现 `order_status` + `_map_order_status`**

`gridtrade/exchanges/binance.py` 的 `cancel_order`(:230-238)之后插入:

```python
    def order_status(self, symbol, order_id) -> str:
        """权威单状态('open'/'filled'/'canceled'/'unknown')。双簿查单:常规簿→algo/trigger 簿
        (同 cancel_order;保险丝是 STOP_MARKET 在 algo 簿,demo 实测常规簿查不到)。两簿皆
        OrderNotFound(古老/purged,极罕见)→ 'unknown',保留调用方 fills 兜底(spec 2026-07-16)。"""
        native = self.to_native(symbol)
        for params in ({}, {'trigger': True}):
            try:
                o = self.client.fetch_order(order_id, native, params)
                return self._map_order_status(o.get('status'))
            except ccxt.OrderNotFound:
                continue
        return 'unknown'

    @staticmethod
    def _map_order_status(ccxt_status) -> str:
        """ccxt 归一化 status → reconciler 三态词表。'open'(NEW/PARTIALLY_FILLED)保'open'——
        在挂优先于成交(与 FakeExchange.order_status 终审语义一致,fake.py:193)。"""
        if ccxt_status == 'closed':
            return 'filled'
        if ccxt_status == 'open':
            return 'open'
        if ccxt_status == 'canceled':
            return 'canceled'
        return 'unknown'
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py -q -o addopts=""`
Expected: PASS(新增 4 个 order_status 测试 + 既有 binance 测试不回归)

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/binance.py tests/exchanges/test_binance_adapter.py
git commit -m "fix(exchanges): 覆写 BinanceAdapter.order_status 双簿查单——修保险丝三态漏判 -2027 背离熔断" \
  -m "未覆写→返基类 'unknown'→reconcile_fuses/E2 补单认不出'已触发/已吃满'→丝成交漏摄入→账本背离熔断(KITE 压测实证)。改双簿 fetch_order(常规→OrderNotFound→trigger,同 cancel_order;丝=STOP_MARKET 在 algo 簿)+ 状态映射(closed→filled/open→open/canceled→canceled/其余→unknown);both-not-found→unknown 保留 fills 兜底。一处修好丝三态+E2三态+churn。spec 2026-07-16 §3。" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 端到端丝触发对账测试(验 ingest 后账本归零、drift=0)

**Files:**
- Test: `tests/execution/test_reconcile_fuses.py`

**Interfaces:**
- Consumes: `BinanceAdapter.order_status`(Task 1;但本测试用 `FakeExchange`,其 order_status 已三态)。验证 `reconcile_fuses`→`ingest_fuse_fills` 把丝成交入账、账本净仓与交易所一致(闭合 KITE bug 的账本背离)。

- [ ] **Step 1: 写端到端失败测试**

在 `tests/execution/test_reconcile_fuses.py` 末尾追加(复用文件内 `_open`/`SYM`/`PARAMS`):

```python
def test_fired_fuse_ingests_fill_ledger_matches_exchange(store):
    # 端到端(spec 2026-07-16 §5,闭合 KITE grid 33b02230 背离):建仓→丝平→reconcile_fuses
    # 摄入丝成交→账本净仓归 0 且与交易所一致(不再 Σclaims≠交易所净仓)。
    from gridtrade.execution.reconciler import Reconciler
    ex, fake, gid = _open(store)
    fake.set_price(SYM, 95.0); ex.sync(gid, SYM)     # 穿部分买线 → 累多仓,fills 入账
    on = ex._geom[gid]['order_num']
    acc0 = ex.accounting.get(gid).net_position
    assert acc0 > 0                                    # 账本已记多仓(基线)
    assert abs(fake.fetch_positions(SYM).net_size - acc0) < 1e-6   # 账本==交易所(基线无背离)
    rec = Reconciler(ex)
    fake.set_price(SYM, 79.0)                          # 穿 stop_low → sell 丝触发(reduce-only 平多)
    out = rec.reconcile_fuses(gid, SYM)
    assert out['fired'] is True                        # 丝被识别为已触发(非误重挂)
    # 核心断言:丝成交已摄入 → 账本净仓归 0、与交易所一致(修复前会卡在 acc0、背离 -> 熔断)
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-6          # 交易所已平(丝 reduce-only)
    assert abs(ex.accounting.get(gid).net_position) < 1e-6         # 账本也归 0(ingest_fuse_fills 生效)
```

- [ ] **Step 2: 跑测试确认状态**

Run: `.venv/bin/python -m pytest tests/execution/test_reconcile_fuses.py::test_fired_fuse_ingests_fill_ledger_matches_exchange -q -o addopts=""`
Expected: PASS(FakeExchange.order_status 已三态 → reconcile_fuses 正确摄入+关格 → 账本归 0)。此为回归护栏:锁死"丝触发→摄入→账本与交易所一致"的正确行为;若 `ingest_fuse_fills`/reconcile 回归,账本不归 0 即挂。

> 注:本测试用 `FakeExchange`(order_status 已正确)故当前即绿,验证的是 reconcile+ingest 下游正确性;Task 1 的单测才验 `BinanceAdapter.order_status` 本身(那是 KITE bug 的真缺口)。二者合起来端到端覆盖:BinanceAdapter 返对状态(单测)→ reconcile 摄入+关格账本一致(本测试)。

- [ ] **Step 3: 回归 + 提交**

Run: `.venv/bin/python -m pytest tests/execution/test_reconcile_fuses.py tests/execution/ -q -o addopts=""`
Expected: PASS(既有丝三态/E2 补单测试全绿,新增端到端绿)

```bash
git add tests/execution/test_reconcile_fuses.py
git commit -m "test(execution): 端到端丝触发对账——建仓→丝平→ingest→账本与交易所一致(闭合 KITE 背离)" \
  -m "补 test_fired_fuse_ingests_fill_ledger_matches_exchange:有仓状态下丝触发,断言 reconcile_fuses 摄入丝成交后账本净仓归 0 且==交易所(既有 test_fired_fuse_tears_down_grid 是 flat 无仓、不覆盖账本背离)。回归护栏锁死 ingest+drift 正确性。spec 2026-07-16 §5。" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- §3.1 双簿行为 → Task 1(常规/trigger 分支测试)✓
- §3.2 order_status 覆写(双簿)→ Task 1 Step 3 ✓
- §3.3 状态映射 `_map_order_status` → Task 1 Step 3 + test_order_status_maps_all_ccxt_statuses ✓
- §3.4 both-not-found→'unknown' → Task 1 test_order_status_both_books_not_found_returns_unknown ✓
- §四 影响面(丝三态/E2/churn 一处修)→ Task 1 覆写即覆盖三消费者;§五回归验既有测试 ✓
- §五 端到端 ingest 验证 → Task 2 ✓
- §六 非目标(恢复暂缓)→ 计划无恢复任务 ✓

**2. Placeholder scan:** 无 TBD/TODO;每步含完整可抄写代码与确切命令/预期。✓

**3. Type consistency:** `order_status(symbol, order_id)->str`、`_map_order_status(ccxt_status)->str` 前后一致;Task 2 用 `_open`/`ex.sync`/`ex.accounting.get`/`ex._geom`/`reconcile_fuses`/`fetch_positions` 均对实体核准存在。✓

**已知非目标(spec 明确):** 已背离格恢复/重摄入、丝/带宽 sizing、E2 补单逻辑本身、mainnet prod。
