# 候选币池移植 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把候选票池补齐到 legacy/文档口径——`list_instruments` 只留 swap 永续并去重（§1）、黑名单无条件生效（§6 档0）、新增可配 24h 成交额绝对地板（③），并把 prod 从白名单切到全市场动态。

**Architecture:** 只改票池构建层（adapter `list_instruments` + `runtime/universe.resolve_live_universe` + 一个新 adapter 方法 `fetch_24h_quote_volumes`）；不碰 offset/选币因子/SymbolLock/记账。地板数据走 ccxt `fetch_tickers().quoteVolume`（交易所无关；HL=`dayNtlVlm` 直传）。

**Tech Stack:** Python 3.9、ccxt 4.5.61、pandas、pytest、FakeExchange 离线测试、fly.io。

## Global Constraints

- 运行测试：`TZ=Asia/Shanghai /Users/thomaschang/Projects/GridTradeGP/.venv/bin/python -m pytest`。
- 依赖冻结（py3.9 / ccxt 4.5.61 / pandas 1.3.5）；不升级。
- `gridtrade/core/` 不 import 交易所库（本计划不碰 core）。
- **不改** offset / 选币因子集与阈值 / SymbolLockGate / 记账 / 现有 55% 成交额过滤。
- `MIN_QUOTE_VOLUME_24H` **code 默认 `0.0`=停用**；$1M 只在 `fly.prod.toml` 显式设。
- 新增 `resolve_live_universe` 的 `min_quote_volume` 参数**默认 0.0**，保持现有调用/测试向后兼容。
- 分支 `universe-port`；**绝不 push `production`**。
- 档1/档2 不实现（SymbolLockGate「每币 ≤1 网格」已覆盖）。

---

### Task 1: §1 — `list_instruments` 只留 swap 永续 + canonical 去重

**Files:**
- Modify: `gridtrade/exchanges/ccxt_adapter.py:29-42`
- Test: `tests/exchanges/test_ccxt_adapter.py`（改 `FakeCcxtClient.markets` + 加新测试）

**Interfaces:**
- Produces: `CcxtAdapter.list_instruments()` 只返回 `m.get('swap') is True` 的市场、按 `to_canonical` 去重（keep first）；`Instrument` schema 不变。

- [ ] **Step 1: 给现有 FakeCcxtClient 的市场补 `swap` 标记 + 写新失败测试**

在 `tests/exchanges/test_ccxt_adapter.py`：把 `FakeCcxtClient.markets` 的 `'BTC/USDT:USDT'` 加上 `'swap': True`（否则新过滤会误伤既有 `test_instruments_mapping`）：

```python
    markets = {'BTC/USDT:USDT': {'swap': True, 'precision': {'price': 0.1, 'amount': 0.001},
                                 'limits': {'amount': {'min': 0.001}},
                                 'active': True, 'info': {'listTime': '0'}}}
```

在文件末尾追加新测试（用一个 canonical 折叠的子类模拟 HL）：

```python
def test_list_instruments_swap_only_and_deduped():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter

    class _FoldClient:
        def load_markets(self):
            return self.markets
        markets = {
            'BTC/USDC:USDC':   {'swap': True,  'precision': {'price': 0.1, 'amount': 0.001},
                                'limits': {'amount': {'min': 0.001}}, 'active': True, 'info': {}},
            'BTC/USDC':        {'swap': False, 'spot': True, 'precision': {}, 'limits': {},
                                'active': True, 'info': {}},                       # spot → 丢
            'ETH/USDC:USDC':   {'swap': True,  'precision': {}, 'limits': {}, 'active': True, 'info': {}},
            'ETH/USDC:USDC-2': {'swap': True,  'precision': {}, 'limits': {}, 'active': True, 'info': {}},  # 折叠成 ETH → 去重
        }

    class _FoldAdapter(CcxtAdapter):
        def to_canonical(self, native):
            return native.split('/')[0] + '/USDC:USDC'

    a = _FoldAdapter(_FoldClient(), name='fold')
    syms = [i.symbol for i in a.list_instruments()]
    assert syms == ['BTC/USDC:USDC', 'ETH/USDC:USDC']   # spot 丢、重复 canonical 去重
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_ccxt_adapter.py::test_list_instruments_swap_only_and_deduped -q`
Expected: FAIL（当前 `list_instruments` 不过滤 swap、不去重 → 返回 4 个含 spot/重复）

- [ ] **Step 3: 实现 swap 过滤 + 去重**

`gridtrade/exchanges/ccxt_adapter.py` 的 `list_instruments`（29-42 行）替换为：

```python
    def list_instruments(self) -> List[Instrument]:
        self.client.load_markets()
        out = []
        seen = set()
        for sym, m in self.client.markets.items():
            if m.get('swap') is not True:          # 只留永续合约，丢 spot/其它类型
                continue
            canonical = self.to_canonical(sym)
            if canonical in seen:                   # 同 canonical 去重（HL spot+swap/多键折叠）
                continue
            seen.add(canonical)
            info = m.get('info', {}) or {}
            out.append(Instrument(
                symbol=canonical,
                tick=float(m.get('precision', {}).get('price') or 0.0),
                lot=float(m.get('precision', {}).get('amount') or 0.0),
                min_size=float(m.get('limits', {}).get('amount', {}).get('min') or 0.0),
                state='live' if m.get('active', True) else 'expired',
                list_ts=int(info.get('listTime') or 0),
            ))
        return out
```

- [ ] **Step 4: 运行确认通过（含既有 mapping 测试）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_ccxt_adapter.py -q`
Expected: PASS（新测试 + `test_instruments_mapping` 均绿）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/ccxt_adapter.py tests/exchanges/test_ccxt_adapter.py
git commit -m "feat(universe): list_instruments 只留 swap 永续 + canonical 去重（§1）"
```

---

### Task 2: §6 — `resolve_live_universe` 黑名单无条件生效

**Files:**
- Modify: `gridtrade/runtime/universe.py:8-14`
- Test: `tests/runtime/test_universe.py`（加测试）

**Interfaces:**
- Produces: `resolve_live_universe(adapter, blacklist=(), whitelist=())` —— blacklist 在**两个分支都先减**（whitelist 模式不再跳过）。

- [ ] **Step 1: 写失败测试（复现白名单模式黑名单失效）**

`tests/runtime/test_universe.py` 末尾追加：

```python
def test_blacklist_applies_even_in_whitelist_mode():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('ETH/USDC:USDC', 'live'),
             ('SOL/USDC:USDC', 'live'))
    # 档0：ETH 被硬禁，即使它在白名单里也必须剔除
    out = resolve_live_universe(ex, blacklist=('ETH/USDC:USDC',),
                                whitelist=('BTC/USDC:USDC', 'ETH/USDC:USDC'))
    assert out == ['BTC/USDC:USDC']
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_universe.py::test_blacklist_applies_even_in_whitelist_mode -q`
Expected: FAIL（当前 whitelist 分支不减 blacklist → 返回 `['BTC','ETH']`）

- [ ] **Step 3: 改为无条件先减 blacklist**

`gridtrade/runtime/universe.py` 的 `resolve_live_universe`（8-14 行）替换为：

```python
def resolve_live_universe(adapter, blacklist=(), whitelist=()) -> List[str]:
    live = [i.symbol for i in adapter.list_instruments() if i.state == 'live']
    live = [s for s in live if s not in set(blacklist)]     # 档0：无条件硬禁（含 whitelist 模式）
    if whitelist:
        return [s for s in live if s in set(whitelist)]
    return live
```

- [ ] **Step 4: 运行确认通过（含现有 universe 测试）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_universe.py -q`
Expected: PASS（新测试 + 5 个现有测试均绿）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/runtime/universe.py tests/runtime/test_universe.py
git commit -m "fix(universe): 黑名单无条件生效（修白名单模式下失效，§6 档0）"
```

---

### Task 3: ③a — adapter 新方法 `fetch_24h_quote_volumes`

**Files:**
- Modify: `gridtrade/exchanges/base.py`（加默认方法）
- Modify: `gridtrade/exchanges/ccxt_adapter.py`（ccxt 实现）
- Modify: `gridtrade/exchanges/resilient_adapter.py`（委托转发）
- Modify: `gridtrade/exchanges/fake.py`（FakeExchange 实现 + seed 钩子）
- Test: `tests/exchanges/test_ccxt_adapter.py`（加测试 + `FakeCcxtClient.fetch_tickers`）

**Interfaces:**
- Produces: `ExchangeAdapter.fetch_24h_quote_volumes() -> Dict[str, float]`（canonical → 24h 计价币成交额；默认空 dict）；`CcxtAdapter` 用 `fetch_tickers()['quoteVolume']`、同 canonical 取最大值；`ResilientAdapter` 转发；`FakeExchange.seed_quote_volumes(dict)` 注入。

- [ ] **Step 1: 写 ccxt 实现的失败测试**

`tests/exchanges/test_ccxt_adapter.py`：给 `FakeCcxtClient` 加一个 `fetch_tickers` 方法（放在类内，紧挨 `fetch_ticker`）：

```python
    def fetch_tickers(self, symbols=None, params=None):
        return {
            'BTC/USDT:USDT': {'quoteVolume': 1000.0},
            'ETH/USDT:USDT': {'quoteVolume': 500.0},
            'NOVOL/USDT:USDT': {'quoteVolume': None},   # 无量 → 跳过
        }
```

文件末尾追加：

```python
def test_fetch_24h_quote_volumes_maps_quotevolume():
    a = _adapter()
    vols = a.fetch_24h_quote_volumes()
    assert vols == {'BTC/USDT:USDT': 1000.0, 'ETH/USDT:USDT': 500.0}   # None 被跳过
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/test_ccxt_adapter.py::test_fetch_24h_quote_volumes_maps_quotevolume -q`
Expected: FAIL（`CcxtAdapter` 无 `fetch_24h_quote_volumes` → AttributeError）

- [ ] **Step 3: base 默认 + ccxt 实现 + resilient 委托 + fake 实现**

`gridtrade/exchanges/base.py`：在 `class ExchangeAdapter` 的「可选：标记价 K线」区（`fetch_mark_ohlcv` 附近）追加默认方法：

```python
    # ---- 可选：24h 成交额（用于流动性地板；默认空=上层跳过过滤）----
    def fetch_24h_quote_volumes(self) -> dict:
        """{canonical symbol: 24h 计价币成交额}。默认空 dict（无数据 → resolve_live_universe fail-open 跳过）。"""
        return {}
```

`gridtrade/exchanges/ccxt_adapter.py`：追加方法（放在 `fetch_price` 之后）：

```python
    def fetch_24h_quote_volumes(self) -> dict:
        tickers = self.client.fetch_tickers()
        out = {}
        for sym, t in tickers.items():
            qv = t.get('quoteVolume')
            if qv is None:
                continue
            canonical = self.to_canonical(sym)
            if float(qv) > out.get(canonical, 0.0):   # 同 canonical(spot+swap 折叠) 取较大者
                out[canonical] = float(qv)
        return out
```

`gridtrade/exchanges/resilient_adapter.py`：在 `fetch_mark_ohlcv` 之后追加：

```python
    def fetch_24h_quote_volumes(self) -> dict:
        return self._call('fetch_24h_quote_volumes')
```

`gridtrade/exchanges/fake.py`：`__init__` 里加存储（在 `self._stops = {}` 附近）`self._quote_volumes = {}`；并在 `list_instruments` 附近追加：

```python
    def seed_quote_volumes(self, vols: dict) -> None:
        self._quote_volumes = dict(vols)

    def fetch_24h_quote_volumes(self) -> dict:
        return dict(self._quote_volumes)
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/exchanges/ -q`
Expected: PASS（新测试绿；既有 exchanges 测试不受影响）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/exchanges/base.py gridtrade/exchanges/ccxt_adapter.py \
        gridtrade/exchanges/resilient_adapter.py gridtrade/exchanges/fake.py \
        tests/exchanges/test_ccxt_adapter.py
git commit -m "feat(universe): adapter.fetch_24h_quote_volumes（ccxt quoteVolume，③a）"
```

---

### Task 4: ③b — 成交额地板接进 `resolve_live_universe` + config + scheduler

**Files:**
- Modify: `gridtrade/runtime/universe.py`（加 `min_quote_volume` 参数 + 过滤）
- Modify: `gridtrade/config.py`（加 `min_quote_volume_24h` 字段 + env 解析）
- Modify: `gridtrade/runtime/scheduler.py:58-59`（透传）
- Test: `tests/runtime/test_universe.py`、`tests/test_config.py`

**Interfaces:**
- Consumes: `adapter.fetch_24h_quote_volumes()`（Task 3）、`FakeExchange.seed_quote_volumes`（Task 3）。
- Produces: `resolve_live_universe(adapter, blacklist=(), whitelist=(), min_quote_volume=0.0)`；`DeployConfig.min_quote_volume_24h: float`（env `MIN_QUOTE_VOLUME_24H`，默认 0.0）。

- [ ] **Step 1: 写成交额地板的失败测试**

`tests/runtime/test_universe.py` 末尾追加：

```python
def test_universe_min_quote_volume_floor():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('MID/USDC:USDC', 'live'),
             ('LOW/USDC:USDC', 'live'), ('NOVOL/USDC:USDC', 'live'))
    ex.seed_quote_volumes({'BTC/USDC:USDC': 5_000_000.0, 'MID/USDC:USDC': 1_000_000.0,
                           'LOW/USDC:USDC': 100_000.0})   # NOVOL 无量
    # 门槛 1e6：保留 >=1e6（BTC/MID）；LOW 与无量 NOVOL 剔除
    out = resolve_live_universe(ex, min_quote_volume=1_000_000.0)
    assert out == ['BTC/USDC:USDC', 'MID/USDC:USDC']


def test_universe_floor_zero_disabled_keeps_all():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('LOW/USDC:USDC', 'live'))
    ex.seed_quote_volumes({'BTC/USDC:USDC': 5_000_000.0, 'LOW/USDC:USDC': 1.0})
    # 门槛 0 = 停用：不过滤（也不管成交额）
    assert resolve_live_universe(ex, min_quote_volume=0.0) == ['BTC/USDC:USDC', 'LOW/USDC:USDC']


def test_universe_floor_failopen_on_empty_volumes():
    from gridtrade.runtime.universe import resolve_live_universe
    ex = _ex(('BTC/USDC:USDC', 'live'), ('LOW/USDC:USDC', 'live'))
    # 未 seed 成交额 → fetch_24h_quote_volumes 返回 {} → fail-open 不清空票池
    assert resolve_live_universe(ex, min_quote_volume=1_000_000.0) == \
           ['BTC/USDC:USDC', 'LOW/USDC:USDC']
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_universe.py::test_universe_min_quote_volume_floor -q`
Expected: FAIL（`resolve_live_universe` 还不接受 `min_quote_volume`）

- [ ] **Step 3: 给 resolve_live_universe 加地板**

`gridtrade/runtime/universe.py` 的 `resolve_live_universe` 替换为：

```python
def resolve_live_universe(adapter, blacklist=(), whitelist=(),
                          min_quote_volume=0.0) -> List[str]:
    live = [i.symbol for i in adapter.list_instruments() if i.state == 'live']
    live = [s for s in live if s not in set(blacklist)]        # 档0：无条件硬禁
    if min_quote_volume and min_quote_volume > 0:              # ③ 绝对成交额地板
        vol = adapter.fetch_24h_quote_volumes()
        if vol:                                                # 空(无数据)→fail-open 跳过、不清空票池
            live = [s for s in live if (vol.get(s) or 0.0) >= min_quote_volume]
    if whitelist:
        return [s for s in live if s in set(whitelist)]
    return live
```

同时更新模块 docstring（1-4 行）末尾补一句：`可选 min_quote_volume>0 时按 24h 成交额过滤（数据缺失则跳过）。`

- [ ] **Step 4: config 加 `min_quote_volume_24h`**

`gridtrade/config.py`：`DeployConfig` 追加字段（放在 `cap_max` 附近的带默认值区）`min_quote_volume_24h: float = 0.0`；`load_deploy_config` 的 return 里追加 `min_quote_volume_24h=_f(env, 'MIN_QUOTE_VOLUME_24H', 0.0),`。

`tests/test_config.py` 追加：

```python
def test_min_quote_volume_24h_default_and_parse():
    assert load_deploy_config(env={}).min_quote_volume_24h == 0.0
    assert load_deploy_config(env={'MIN_QUOTE_VOLUME_24H': '1000000'}).min_quote_volume_24h == 1_000_000.0
```

- [ ] **Step 5: scheduler 透传**

`gridtrade/runtime/scheduler.py` 58-59 行改为：

```python
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist,
                                     rt.config.whitelist, rt.config.min_quote_volume_24h)
```

- [ ] **Step 6: 运行确认通过（全套）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/runtime/test_universe.py tests/test_config.py tests/runtime/test_scheduler.py -q`
Expected: PASS（新 3 个 universe 测试 + config 测试 + scheduler 现有测试均绿）

- [ ] **Step 7: 提交**

```bash
git add gridtrade/runtime/universe.py gridtrade/config.py gridtrade/runtime/scheduler.py \
        tests/runtime/test_universe.py tests/test_config.py
git commit -m "feat(universe): 24h 成交额绝对地板接线（config+scheduler，③b）"
```

---

### Task 5: prod config 去白名单 + 设地板

**Files:**
- Modify: `deploy/fly.prod.toml`

**Interfaces:** 无代码接口（部署 env）。

- [ ] **Step 1: 改 fly.prod.toml**

在 `deploy/fly.prod.toml` 的 `[env]` 段：
1. **删除** `UNIVERSE_WHITELIST = "..."` 整行（切全市场动态）。
2. **追加** `  MIN_QUOTE_VOLUME_24H = "1000000"`（$1M 绝对地板）。
3. **追加** `  BLACKLIST_SYMBOLS = ""`（档0 硬禁名单，留空=不禁；上线前由用户填 HL coin 符号，如 `"XXX/USDC:USDC,YYY/USDC:USDC"`）。

- [ ] **Step 2: 校验无残留 whitelist + 地板已设**

Run: `grep -nE 'UNIVERSE_WHITELIST|MIN_QUOTE_VOLUME_24H|BLACKLIST_SYMBOLS' deploy/fly.prod.toml`
Expected: **无** `UNIVERSE_WHITELIST` 行；**有** `MIN_QUOTE_VOLUME_24H = "1000000"` 与 `BLACKLIST_SYMBOLS = "..."`。

- [ ] **Step 3: 提交**

```bash
git add deploy/fly.prod.toml
git commit -m "feat(deploy): prod 去 UNIVERSE_WHITELIST 走全市场 + MIN_QUOTE_VOLUME_24H=1M + BLACKLIST"
```

---

### Task 6: 文档/记忆同步

**Files:**
- Modify: `docs/STATUS.md`
- Modify: `docs/superpowers/specs/2026-07-04-candidate-universe-port-design.md`（状态）
- Create: 记忆 `/Users/thomaschang/.claude/projects/-Users-thomaschang-Projects-GridTradeGP/memory/candidate-universe-port.md` + `MEMORY.md` 追加一行（由控制器写；实现子代理跳过记忆）

- [ ] **Step 1: STATUS.md 加币池条目**

`docs/STATUS.md` §8 gotchas 追加：

```markdown
- **候选票池**：`list_instruments` 只留 swap 永续 + canonical 去重；`resolve_live_universe` 黑名单无条件生效（含白名单模式）；可配 `MIN_QUOTE_VOLUME_24H` 绝对成交额地板（ccxt `quoteVolume`，code 默认 0=停用，prod 设 $1M）。**prod 已去 `UNIVERSE_WHITELIST` 走全市场动态**（全部永续 −黑名单 −24h成交额<$1M → 选币再 55%相对过滤）。档1/档2 由 SymbolLockGate 覆盖不实现。
```

- [ ] **Step 2: 更新 spec 状态**

`docs/superpowers/specs/2026-07-04-candidate-universe-port-design.md` 顶部 `状态：设计已确认，待写实现计划` → `状态：已实现（代码合入前，待 testnet 验证）`。

- [ ] **Step 3: 提交**

```bash
git add docs/STATUS.md docs/superpowers/specs/2026-07-04-candidate-universe-port-design.md
git commit -m "docs(universe): STATUS/spec 同步候选币池移植"
```

---

### Task 7: testnet 验证（ops，非代码）

**前置：** Task 1-6 合入 main、全套绿。

- [ ] **Step 1: 合并 + 触发 testnet CD**

```bash
git checkout main && git merge --no-ff universe-port && git push origin main
gh workflow run deploy.yml        # testnet gridtrade-hl
```

- [ ] **Step 2: 临时把 testnet 也切全市场 + 地板验证机制**（testnet 成交额是假的，地板阈值须 testnet 特调，不用 $1M）

```bash
# 用 fly secrets/env 临时覆盖 testnet（不提交）：去 whitelist、设一个 testnet 能过的小地板
fly secrets set UNIVERSE_WHITELIST="" MIN_QUOTE_VOLUME_24H="0" -a gridtrade-hl   # 先只验去重+黑名单，地板另测
```

- [ ] **Step 3: 观察**

Run: `bash scripts/testnet_status.sh` + `fly logs -a gridtrade-hl --no-tail | grep -E 'scheduler|skipped|universe|gate|degraded|Traceback' | head -40`
确认：universe 变大（全市场）但 scheduler 单轮耗时可接受、坏币 try/except 跳过、黑名单剔除生效、无 traceback、选币照常出网格。

- [ ] **Step 4: 确认后上 prod**

testnet 机制验证 OK 后，按 `deploy/DEPLOY.md`：`main → production` 合并 → `git push origin production`（= 真钱，谨慎；prod 用 $1M 地板）。上线后盯首轮 universe 规模 + scheduler 耗时 + 选币。

---

## 自查（对照 spec）

- **Spec 覆盖**：§1=Task1；§6档0=Task2；③地板=Task3(方法)+Task4(接线)；prod去白名单=Task5；文档=Task6；testnet先行=Task7。✅
- **占位符扫描**：无 TBD；每改码步骤含完整代码。`BLACKLIST_SYMBOLS=""` 是有意留空（内容=用户策略），非占位。
- **类型一致**：`resolve_live_universe(adapter, blacklist, whitelist, min_quote_volume=0.0)`、`fetch_24h_quote_volumes()->dict`、`seed_quote_volumes(dict)`、`DeployConfig.min_quote_volume_24h`、scheduler 4 参调用——定义与调用一致。✅
