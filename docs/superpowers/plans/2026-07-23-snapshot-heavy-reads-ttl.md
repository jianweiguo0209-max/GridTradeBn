# snapshot 重读降频（income TTL + algo 簿 TTL）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** BinanceAdapter 内给 income（资金费流水）加 300s TTL 缓存、给保险丝 algo 簿加 60s TTL 缓存，砍掉 monitor 基线 ~280 权重/min。

**Architecture:** 缓存全部落在 `BinanceAdapter`（两簿拆分/income 单流是它的私有知识）；income 缓存带参数语义命中规则（TTL 内 + 请求 since ≥ 缓存 since + 请求 symbols ⊆ 缓存 symbols，任一不满足即真取）；algo 簿账户级无参数、`create_stop_order` 成功后主动失效；TTL 经 config env → factory dict → registry → from_credentials → __init__ 注入，`<=0` = 关闭缓存逐字节恢复旧行为。

**Tech Stack:** Python 3.9、pytest、既有 FakeBinanceClient 测试桩。

**Spec:** `docs/superpowers/specs/2026-07-23-snapshot-heavy-reads-ttl-design.md`

## Global Constraints

- Python 3.9 兼容（禁 3.10+ 语法）
- 注释中文、讲"为什么"；日志 `%` 格式化
- **常规簿每次真取不得缓存**（判成交核心）；userTrades/positions/prices/balance 一律不动
- income 契约不变：返回键 = 请求 symbols 全集（缺省空列表）、每 symbol 升序、"支付为正"
- 真取失败原样上抛、**不写缓存**（snapshot 构建失败整轮跳过 = 现行为）
- TTL `<=0` = 关闭缓存（每次真取，旧行为）
- 时钟注入：`now_fn=time.time` kwarg（跟随 signals.py 惯例，测试不 monkeypatch）
- TDD：先失败测试再实现；提交信息末尾 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- 部署硬规则：合 main → push origin → merge main→production → push 走 CI/CD；部署前后 verify-ledger

---

### Task 1: income TTL 缓存

**Files:**
- Modify: `gridtrade/exchanges/binance.py`（`__init__`、`fetch_funding_payments_all` 拆缓存壳+真取体）
- Test: `tests/exchanges/test_snapshot_read_ttl.py`（新建）

**Interfaces:**
- Consumes: 既有 `FakeBinanceClient`（`tests/exchanges/test_binance_adapter.py`——`fapiPrivateGetIncome` 自带 `income_calls` 计数，返回 BTC ts=2000 / ETH ts=1000 两行）
- Produces: `BinanceAdapter.__init__(self, client, *, income_ttl_sec=300.0, algo_book_ttl_sec=60.0, now_fn=time.time)`；实例属性 `income_ttl_sec`/`algo_book_ttl_sec`/`_income_cache`/`_algo_book_cache`（Task 2/3 依赖）；`_fetch_funding_payments_fresh(symbols, since_ms=None)`（原真取体改名）

- [ ] **Step 1: Write the failing tests**

新建 `tests/exchanges/test_snapshot_read_ttl.py`：

```python
"""snapshot 重读降频：income TTL + algo 簿 TTL（spec 2026-07-23-snapshot-heavy-reads-ttl）。"""
from tests.exchanges.test_binance_adapter import FakeBinanceClient


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _adapter(client, clock, income_ttl=300.0, algo_ttl=60.0):
    from gridtrade.exchanges.binance import BinanceAdapter
    return BinanceAdapter(client, income_ttl_sec=income_ttl,
                          algo_book_ttl_sec=algo_ttl, now_fn=clock)


SYM2 = ['BTC/USDT:USDT', 'ETH/USDT:USDT']


def _flat(out):
    return {s: [(p.ts, p.amount) for p in v] for s, v in out.items()}


def test_income_ttl_hit_single_fetch_same_result():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)
    first = a.fetch_funding_payments_all(SYM2, since_ms=500)
    clk.t += 10.0
    second = a.fetch_funding_payments_all(SYM2, since_ms=500)
    assert len(c.income_calls) == 1              # TTL 内第二调命中缓存
    assert _flat(first) == _flat(second)


def test_income_hit_filters_by_later_since():
    # cursor 前进：命中时按请求 since 本地切片（ETH ts=1000 应被 since=1500 滤掉）
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)
    a.fetch_funding_payments_all(SYM2, since_ms=500)
    out = a.fetch_funding_payments_all(SYM2, since_ms=1500)
    assert len(c.income_calls) == 1
    assert [p.ts for p in out['BTC/USDT:USDT']] == [2000]
    assert out['ETH/USDT:USDT'] == []            # 键仍在（契约：请求 symbols 全集）


def test_income_since_regression_busts_cache():
    # 新开格 cursor=0 把 since 拉回 → 必须击穿缓存真取（漏记防线）
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)
    a.fetch_funding_payments_all(SYM2, since_ms=1500)
    out = a.fetch_funding_payments_all(SYM2, since_ms=0)
    assert len(c.income_calls) == 2
    assert [p.ts for p in out['ETH/USDT:USDT']] == [1000]   # 拉回后旧行可见


def test_income_symbols_superset_busts_cache():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)
    a.fetch_funding_payments_all(['BTC/USDT:USDT'], since_ms=0)
    out = a.fetch_funding_payments_all(SYM2, since_ms=0)     # 新币入快照 → miss
    assert len(c.income_calls) == 2
    assert 'ETH/USDT:USDT' in out


def test_income_ttl_expiry_refetches():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk, income_ttl=300.0)
    a.fetch_funding_payments_all(SYM2, since_ms=0)
    clk.t += 301.0
    a.fetch_funding_payments_all(SYM2, since_ms=0)
    assert len(c.income_calls) == 2


def test_income_disabled_when_ttl_nonpositive():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk, income_ttl=0.0)
    a.fetch_funding_payments_all(SYM2, since_ms=0)
    a.fetch_funding_payments_all(SYM2, since_ms=0)
    assert len(c.income_calls) == 2              # 关闭=每次真取（旧行为）


def test_income_fetch_error_propagates_and_not_cached():
    c, clk = FakeBinanceClient(), _Clock()
    a = _adapter(c, clk)

    def boom(params=None):
        raise RuntimeError('income down')
    c.fapiPrivateGetIncome = boom
    import pytest
    with pytest.raises(RuntimeError):
        a.fetch_funding_payments_all(SYM2, since_ms=0)
    assert a._income_cache is None               # 失败不污染缓存
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/exchanges/test_snapshot_read_ttl.py -v`
Expected: 全部 FAIL with `TypeError: __init__() got an unexpected keyword argument 'income_ttl_sec'`

- [ ] **Step 3: Implement**

`gridtrade/exchanges/binance.py`：文件头 import 区补 `import time`（若无）。`__init__` 替换为：

```python
    def __init__(self, client, *, income_ttl_sec=300.0, algo_book_ttl_sec=60.0,
                 now_fn=time.time):
        super().__init__(client, name='binance')
        # snapshot 重读降频(spec 2026-07-23):income 资金费 8h 才结算、algo 簿(保险丝)
        # 极少变——13s 快照轮逐次真取纯浪费(遥测实测二者合计 ~280 权重/min)。
        # <=0 = 关闭缓存,每次真取,逐字节恢复旧行为。
        self.income_ttl_sec = float(income_ttl_sec)
        self.algo_book_ttl_sec = float(algo_book_ttl_sec)
        self._now = now_fn
        self._income_cache = None      # (fetched_at, since_used, symbols_used:set, grouped:dict)
        self._algo_book_cache = None   # (fetched_at, rows:list)
```

`fetch_funding_payments_all` 拆为缓存壳 + 真取体（真取体 = 现方法体**原样搬移**改名，
docstring 保留原分页/去重说明）：

```python
    def fetch_funding_payments_all(self, symbols, since_ms=None):
        """income(FUNDING_FEE) 账户级单流(权重30) + TTL 缓存(spec 2026-07-23)。
        命中三条件=TTL 内+请求 since≥缓存 since+请求 symbols⊆缓存 symbols;任一不满足
        (新开格 cursor=0 拉回 since/新币入快照)→真取,正确性自保。命中时本地按请求
        since/symbols 切片,契约不变(键=请求 symbols 全集,缺省空列表,升序)。
        真取失败原样上抛且不写缓存(快照构建失败整轮跳过=现行为)。"""
        req_since = 0 if since_ms is None else int(since_ms)
        c = self._income_cache
        if (self.income_ttl_sec > 0 and c is not None
                and (self._now() - c[0]) < self.income_ttl_sec
                and req_since >= c[1] and set(symbols) <= c[2]):
            grouped = c[3]
            return {s: [p for p in grouped.get(s, []) if p.ts >= req_since]
                    for s in symbols}
        out = self._fetch_funding_payments_fresh(symbols, since_ms=since_ms)
        self._income_cache = (self._now(), req_since, set(symbols), out)
        return out

    def _fetch_funding_payments_fresh(self, symbols, since_ms=None):
        """（原 fetch_funding_payments_all 方法体原样搬移，含原 docstring 的
        分页含边界重取+tranId 去重+防死转说明——此处一行不改。）"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/exchanges/test_snapshot_read_ttl.py tests/exchanges/test_binance_adapter.py tests/exchanges/test_account_batch_base.py -v`
Expected: 全部 PASS（既有 income 用例走 miss 路径行为不变；默认构造 `BinanceAdapter(client)` 仍合法）。

- [ ] **Step 5: Commit**

```bash
git add tests/exchanges/test_snapshot_read_ttl.py gridtrade/exchanges/binance.py
git commit -m "feat(binance): income资金费流水TTL缓存——带since/symbols命中规则防漏记

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: algo 簿 TTL 缓存 + create_stop_order 写失效

**Files:**
- Modify: `gridtrade/exchanges/binance.py`（`fetch_open_orders_all`、`create_stop_order` 尾部）
- Test: `tests/exchanges/test_snapshot_read_ttl.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `self.algo_book_ttl_sec` / `self._algo_book_cache` / `self._now`；测试沿用 `_Clock`/`_adapter`
- Produces: `fetch_open_orders_all(symbols)` 行为不变但 algo 簿按 TTL 复用；`create_stop_order` 成功后 `self._algo_book_cache = None`

- [ ] **Step 1: Write the failing tests**

`tests/exchanges/test_snapshot_read_ttl.py` 追加（挂单行字段形状抄自
`tests/exchanges/test_binance_adapter.py::test_fetch_open_orders_merges_trigger_book`
的桩返回——若 `_to_order` 对下述简化行报 KeyError，以该既有测试的行形状为准对齐）：

```python
class _BookClient(FakeBinanceClient):
    """两簿分开计数：常规簿必须每调真取，algo 簿按 TTL 复用。"""
    def __init__(self):
        super().__init__()
        self.regular_calls = 0
        self.trigger_calls = 0

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        if params and params.get('trigger'):
            self.trigger_calls += 1
            return [{'id': '9', 'clientOrderId': '', 'symbol': 'BTC/USDT:USDT',
                     'side': 'sell', 'price': 40000.0, 'amount': 1.0,
                     'status': 'open', 'filled': 0.0}]
        self.regular_calls += 1
        return [{'id': '7', 'clientOrderId': '', 'symbol': 'BTC/USDT:USDT',
                 'side': 'buy', 'price': 49000.0, 'amount': 1.0,
                 'status': 'open', 'filled': 0.0}]

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        return {'id': '11', 'clientOrderId': '', 'symbol': 'BTC/USDT:USDT',
                'side': side, 'price': 0.0, 'amount': amount,
                'status': 'open', 'filled': 0.0}


def test_algo_book_cached_regular_always_fresh():
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    clk.t += 10.0
    out = a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.regular_calls == 2                  # 常规簿每调真取（判成交核心）
    assert c.trigger_calls == 1                  # algo 簿 TTL 内复用
    assert sorted(o.id for o in out) == ['7', '9']   # merge 结果不变


def test_algo_ttl_expiry_refetches():
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    clk.t += 61.0
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.trigger_calls == 2


def test_algo_cache_invalidated_by_create_stop_order():
    # 挂新丝 → 缓存失效 → 下一轮 algo 簿真取（新丝立即可见，省 order_status 兜底链）
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    a.create_stop_order('BTC/USDT:USDT', 'sell', 1.0, 40000.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.trigger_calls == 2


def test_algo_disabled_when_ttl_nonpositive():
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=0.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert c.trigger_calls == 2                  # 关闭=每次真取（旧行为）


def test_algo_ghost_row_persists_within_ttl_then_clears():
    # 撤丝后的幽灵行：TTL 窗内仍可见（三态判只看存在性,不会因幽灵行动作）,到期后消失。
    # 这是 spec 预注册的可接受行为——为它立契约,防未来有人"顺手"给撤单也加失效钩子
    # 时误以为现状是 bug。
    c, clk = _BookClient(), _Clock()
    a = _adapter(c, clk, algo_ttl=60.0)
    a.fetch_open_orders_all(['BTC/USDT:USDT'])           # 缓存含丝 '9'
    c.fetch_open_orders = (lambda symbol=None, since=None, limit=None, params=None:
                           [] if (params and params.get('trigger'))
                           else [{'id': '7', 'clientOrderId': '',
                                  'symbol': 'BTC/USDT:USDT', 'side': 'buy',
                                  'price': 49000.0, 'amount': 1.0,
                                  'status': 'open', 'filled': 0.0}])  # 交易所侧丝已撤
    clk.t += 10.0
    out = a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert '9' in {o.id for o in out}                    # TTL 窗内幽灵行仍在
    clk.t += 61.0
    out = a.fetch_open_orders_all(['BTC/USDT:USDT'])
    assert '9' not in {o.id for o in out}                # 到期真取后消失
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/exchanges/test_snapshot_read_ttl.py -v -k "algo"`
Expected: `test_algo_book_cached_regular_always_fresh`/`test_algo_cache_invalidated_by_create_stop_order` FAIL（trigger_calls 计数不符——现实现每调真取）；其余两条可能 PASS（回归护栏，保留）。

- [ ] **Step 3: Implement**

`gridtrade/exchanges/binance.py` `fetch_open_orders_all` 替换为：

```python
    def fetch_open_orders_all(self, symbols):
        # 账户级两簿并读(常规40+algo40 权重)。algo 簿=保险丝对账专用、极少变→TTL 缓存
        # (spec 2026-07-23);常规簿判成交核心,永远真取。create_stop_order 挂新丝时主动
        # 失效缓存(新丝下轮立即可见);撤丝后的幽灵行由三态判存在性语义+TTL 到期自愈吸收,
        # 保险丝重挂有 order_status 权威判+streak 守卫双兜底,陈旧簿不会导致重复挂丝。
        want = set(symbols)
        rows = list(self.client.fetch_open_orders(None))
        c = self._algo_book_cache
        if (self.algo_book_ttl_sec > 0 and c is not None
                and (self._now() - c[0]) < self.algo_book_ttl_sec):
            algo_rows = c[1]
        else:
            algo_rows = list(self.client.fetch_open_orders(None, params={'trigger': True}))
            self._algo_book_cache = (self._now(), algo_rows)
        rows = rows + algo_rows
        return [o for o in (self._to_order(r) for r in rows) if o.symbol in want]
```

`create_stop_order` 尾部（现为 `r = self.client.create_order(...)` 后直接
`return self._to_order(r)`）改为：

```python
        r = self.client.create_order(self.to_native(symbol), 'market', side, size,
                                     None, p)
        self._algo_book_cache = None   # 新丝立即可见：失效 algo 簿缓存(spec 2026-07-23)
        return self._to_order(r)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/exchanges/test_snapshot_read_ttl.py tests/exchanges/test_binance_adapter.py tests/exchanges/ -q`
Expected: 全部 PASS（既有 `test_fetch_open_orders_merges_trigger_book` 走 miss 路径 merge 语义不变；`fetch_open_orders(symbol)` 单币两簿并读未动）。

- [ ] **Step 5: Commit**

```bash
git add tests/exchanges/test_snapshot_read_ttl.py gridtrade/exchanges/binance.py
git commit -m "feat(binance): 保险丝algo簿TTL缓存+挂丝写失效——常规簿保持每轮真取

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 配置接线（env → factory → registry → from_credentials）

**Files:**
- Modify: `gridtrade/config.py`（DeployConfig 两字段 + env 解析两行，放 `signal_refresh_sec` 旁）
- Modify: `gridtrade/exchanges/registry.py:12-16`（from_credentials 传参）
- Modify: `gridtrade/exchanges/binance.py`（`from_credentials` 签名加 kwargs 透传）
- Modify: `gridtrade/runtime/factory.py:61-67`（build_adapter dict 加两键）
- Test: `tests/test_config.py`（追加）、`tests/exchanges/test_snapshot_read_ttl.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `__init__(client, *, income_ttl_sec, algo_book_ttl_sec, now_fn)`
- Produces: env `SNAPSHOT_INCOME_TTL_SEC`(默认300)/`SNAPSHOT_ALGO_BOOK_TTL_SEC`(默认60) → config 字段 `snapshot_income_ttl_sec`/`snapshot_algo_book_ttl_sec` → 实例属性

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py` 末尾追加（该文件既有惯例：`load_deploy_config(env={...})`，
文件头已 import）：

```python
def test_snapshot_ttl_env_parsing_and_defaults():
    cfg = load_deploy_config(env={'SNAPSHOT_INCOME_TTL_SEC': '120',
                                  'SNAPSHOT_ALGO_BOOK_TTL_SEC': '30'})
    assert cfg.snapshot_income_ttl_sec == 120.0
    assert cfg.snapshot_algo_book_ttl_sec == 30.0
    cfg2 = load_deploy_config(env={})
    assert cfg2.snapshot_income_ttl_sec == 300.0
    assert cfg2.snapshot_algo_book_ttl_sec == 60.0
```

`tests/exchanges/test_snapshot_read_ttl.py` 追加：

```python
def test_from_credentials_passes_ttls():
    from gridtrade.exchanges.binance import BinanceAdapter
    a = BinanceAdapter.from_credentials('k', 's', income_ttl_sec=5.0,
                                        algo_book_ttl_sec=7.0)
    assert a.income_ttl_sec == 5.0 and a.algo_book_ttl_sec == 7.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py -v -k "snapshot_ttl" && .venv/bin/python -m pytest tests/exchanges/test_snapshot_read_ttl.py -v -k "credentials"`
Expected: config 两条 FAIL（无该字段）；credentials 条 FAIL（unexpected keyword）。

- [ ] **Step 3: Implement**

`gridtrade/config.py`：DeployConfig dataclass 在 `signal_refresh_sec` 字段旁加：

```python
    # snapshot 重读降频(spec 2026-07-23):income/algo 簿 TTL 秒;<=0=关闭缓存(旧行为)
    snapshot_income_ttl_sec: float = 300.0
    snapshot_algo_book_ttl_sec: float = 60.0
```

env 解析处（`signal_refresh_sec=_f(...)` 行旁）加：

```python
        snapshot_income_ttl_sec=_f(env, 'SNAPSHOT_INCOME_TTL_SEC', 300.0),
        snapshot_algo_book_ttl_sec=_f(env, 'SNAPSHOT_ALGO_BOOK_TTL_SEC', 60.0),
```

`gridtrade/exchanges/binance.py` `from_credentials` 签名与收尾：

```python
    def from_credentials(cls, api_key, secret, *, testnet=False, proxies=None,
                         timeout=10000, income_ttl_sec=300.0, algo_book_ttl_sec=60.0):
        ...（现体不动）...
        return cls(client, income_ttl_sec=income_ttl_sec,
                   algo_book_ttl_sec=algo_book_ttl_sec)
```

`gridtrade/exchanges/registry.py` binance 分支：

```python
        adapter = BinanceAdapter.from_credentials(
            config.get('api_key', ''), config.get('secret', ''),
            testnet=bool(config.get('testnet', False)),
            proxies=config.get('proxies'),
            income_ttl_sec=float(config.get('income_ttl_sec', 300.0)),
            algo_book_ttl_sec=float(config.get('algo_book_ttl_sec', 60.0)))
```

`gridtrade/runtime/factory.py` `build_adapter({...})` dict 加两键：

```python
        'income_ttl_sec': getattr(config, 'snapshot_income_ttl_sec', 300.0),
        'algo_book_ttl_sec': getattr(config, 'snapshot_algo_book_ttl_sec', 60.0),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/exchanges/test_snapshot_read_ttl.py tests/runtime/test_factory.py -q`
Expected: 全部 PASS（fake 交易所路径不受 dict 新键影响——build_adapter 对 fake 分支忽略之）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/config.py gridtrade/exchanges/binance.py gridtrade/exchanges/registry.py gridtrade/runtime/factory.py tests/test_config.py tests/exchanges/test_snapshot_read_ttl.py
git commit -m "feat(config): SNAPSHOT_INCOME_TTL_SEC/SNAPSHOT_ALGO_BOOK_TTL_SEC接线到BinanceAdapter

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 全量验证 + 部署（CI/CD）

**Files:** 无新改动；验证 + 发布

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: 全绿（基线 1077+新增，0 failed）。任何红先修复，禁止带红部署。

- [ ] **Step 2: 部署前 verify-ledger**

```bash
flyctl ssh console -a gridtrade-bi-prod -C "python -m gridtrade.runtime.dbadmin verify-ledger"
```
Expected: `pairs_bad=0 replay_bad=0 symbol_drift=0`。

- [ ] **Step 3: 推 main + 合 production 触发 CD（⚠ 此步执行前需用户确认）**

```bash
git push origin main
git fetch origin production && git log --oneline origin/production -1
git checkout production && git pull origin production
git merge main
git push origin production
git checkout main
```

- [ ] **Step 4: 盯 CD 与部署后验证**

```bash
gh run list --repo rockingchang/GrideTradeBi --limit 2   # Deploy Mainnet → success
flyctl status -a gridtrade-bi-prod                        # 版本+1、全 started
```
验收（对照 spec）：
- monitor `[weight]` 线 **w1m 水位下降**（calls/min 计数不变是预期——计的是逻辑调用，勿误读）
- 整点选币分钟 w1m ~1506 → ~1230
- 429/CircuitOpenError 降级窗频率下降（基线 ~2窗/15min）

- [ ] **Step 5: 部署后 verify-ledger**

```bash
flyctl ssh console -a gridtrade-bi-prod -C "python -m gridtrade.runtime.dbadmin verify-ledger"
```
Expected: clean。观察 1-2 个资金费结算点（00/08/16 UTC）后确认 funding 记账无漏（晚 ≤5min 可接受）。
