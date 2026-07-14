# 保险丝覆盖率保障 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保险丝（reduce-only STOP_MARKET）数量受币安 `MARKET_LOT_SIZE.maxQty` 限制时，开仓前把 cap 降到"丝能护全额"的水平（保住币、只缩仓），降到 `CAP_MIN` 之下才拒币——权益自适应，主网当前恒不触发。

**Architecture:** 数据面给 `Instrument` 加 `market_max_qty`（沿 `min_cost` 先例，零额外 API）；纯函数 `execution/fuse_policy.py` 算覆盖率与降档 cap；门链新增 `FuseCoverageGate`（与 `MinNotionalGate` 完全同构），把定稿 cap 写回 `proposal.cap`；降后"每笔名义额够不够"交给链上紧随其后的 `MinNotionalGate` 自然拒（DRY）。适配器封顶（ed4616e）与 `reconcile_fuses` 均不改。

**Tech Stack:** Python 3.9、ccxt 4.5.61、pytest。无新依赖。

**Spec:** `docs/superpowers/specs/2026-07-15-fuse-coverage-guard-design.md`（已批准；本计划引用其章节号）。

## Global Constraints

- Python 3.9 / pandas 1.3.5 / numpy 1.22.4 锁死；同步架构；中文注释 + spec 引用（与仓库既有风格一致）。
- **口径唯一事实源**：`worst = 每笔数量 × grid_count`，`每笔数量` 来自 `grid_order_info(cap, gearing, low, high, grid_count, stop_low, stop_high, min_amount=..., max_rate=1.0)`——**`max_rate=1.0`**（实盘 `grid_executor.py:84` 用的就是 1.0，不是回测的 0.68）。任何新代码算 worst 必须同源。
- **`min_coverage` 是"干预触发阈值"，一旦干预就降到足额**（`coverage'=1.0`）——不存在"降到 80% 就收手"的中间态（spec §四）。
- **fail-open 三处**：`market_max_qty` 未知（≤0）→ 不干预；`list_instruments()` 抛异常 → 空映射不干预；`grid_order_info` 返 None（cap 太低建不了网）→ 不干预（交给 `MinNotionalGate` 拒）。**绝不因限额表读不到而拒单。**
- **不改**：适配器封顶逻辑（`create_stop_order` 的 clamp，防 -4005 最后一道）、`reconcile_fuses`、`grids` 表 schema、回测几何。
- `FUSE_MIN_COVERAGE` 默认 `1.0`；两个 fly toml 均设 `"1.0"`（用户定：demo 上也真实触发，不做主网才启用的死代码）。
- 测试命令：`.venv/bin/python -m pytest <path> -q -o addopts=""`（`-o addopts=""` 是本仓惯例，避免 `-q` 摘要行被吞）。
- 每个任务末尾 commit，消息风格 `feat(scope): 中文摘要(spec 2026-07-15 §N)`，末尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- **不部署**（部署由运维会话按"避开整点 HH:00–HH:12"手动做）。

## 关键事实（实测，编码时直接引用勿再查）

- 主网票池（291 币）最小市价名义上限 **$30,570**；满仓名义额 = `cap × gearing` = `equity × 0.8333`（frac 0.2451 × gearing 3.4）⇒ **临界权益 ≈ $36,684**，其下零不足额币。
- demo 的 maxQty 比主网小 3–1200 倍（HMSTR 57×、PORTAL 167×）⇒ demo 上本机制会真实触发。
- 既有测试桩：`tests/exchanges/test_binance_adapter.py:15` 的 `FakeBinanceClient` BTC markets 已含 `'market': {'min': 0.001, 'max': 120.0}`，ETH **无** `market` 键（fail-open 用例）。
- `tests/exchanges/test_ccxt_adapter.py:54-56` 的 `FakeCcxtClient.markets` 只有 `amount`/`cost` limits，**无 `market` 键**（Task 1 要加）。
- `GridExecutor` 已有属性：`gearing`、`min_amount`、`cap_min`、`_resolve_cap()`。
- `GridProposal` 已有 `cap` 字段；`RiskBudgetGate`/`MarginGate`/`executor.open`/`LiveEquity` 都已 honor 它。
- `ResilientAdapter.list_instruments` 已转发（`_call('list_instruments')`）⇒ 新字段自动流过，**无需改转发**。

---

### Task 1: 数据面 —— `Instrument.market_max_qty`（spec §三）

**Files:**
- Modify: `gridtrade/exchanges/base.py`（`Instrument` dataclass）
- Modify: `gridtrade/exchanges/ccxt_adapter.py`（`list_instruments`）
- Test: `tests/exchanges/test_ccxt_adapter.py`

**Interfaces:**
- Produces: `Instrument.market_max_qty: float = 0.0`（**追加在字段末尾**，位置参构造兼容不破）；`CcxtAdapter.list_instruments()` 从 ccxt `limits.market.max` 填充（缺失=0.0）。
- Consumes: 无。

- [ ] **Step 1: 给测试桩加 market limits，写失败测试**

在 `tests/exchanges/test_ccxt_adapter.py` 把 `FakeCcxtClient.markets`（54-56 行）改为——**新增第二个币 ETH 作缺失用例**：

```python
    markets = {'BTC/USDT:USDT': {'swap': True, 'precision': {'price': 0.1, 'amount': 0.001},
                                 'limits': {'amount': {'min': 0.001}, 'cost': {'min': 5.0},
                                            'market': {'min': 0.001, 'max': 120.0}},
                                 'active': True, 'info': {'listTime': '0'}},
               'ETH/USDT:USDT': {'swap': True, 'precision': {'price': 0.01, 'amount': 0.01},
                                 'limits': {'amount': {'min': 0.01}, 'cost': {'min': 20.0}},
                                 'active': True, 'info': {'listTime': '0'}}}
```

在同文件末尾追加：

```python
def test_list_instruments_fills_market_max_qty():
    # 市价单单笔数量上限（币安 MARKET_LOT_SIZE.maxQty，ccxt limits.market.max）——
    # 保险丝覆盖率门的数据面（spec 2026-07-15 §三）
    insts = {i.symbol: i for i in _adapter().list_instruments()}
    assert insts['BTC/USDT:USDT'].market_max_qty == 120.0
    assert insts['ETH/USDT:USDT'].market_max_qty == 0.0   # 缺 market 键 → 0=未知（fail-open）


def test_instrument_market_max_qty_defaults_zero():
    from gridtrade.exchanges.base import Instrument
    i = Instrument(symbol='X/USDT:USDT', tick=0.1, lot=0.1, min_size=0.1,
                   state='live', list_ts=0)
    assert i.market_max_qty == 0.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_ccxt_adapter.py -q -o addopts=""`
预期：2 个新测试 FAIL（`Instrument` 无 `market_max_qty` 属性）。既有 `test_list_instruments_fills_min_cost` 仍须 PASS（markets 改动不得破坏它）。

- [ ] **Step 3: 实现**

`gridtrade/exchanges/base.py` 的 `Instrument` **末尾追加一个字段**（保持既有字段与注释不动）：

```python
    market_max_qty: float = 0.0  # 市价单单笔数量上限（币安 MARKET_LOT_SIZE.maxQty；0=未知/无约束→fail-open）
```

`gridtrade/exchanges/ccxt_adapter.py` 的 `list_instruments()` 里 `out.append(Instrument(...))` **增加一行参数**（紧跟既有 `min_cost=` 行）：

```python
                market_max_qty=float(((m.get('limits', {}) or {}).get('market', {}) or {}).get('max') or 0.0),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/ -q -o addopts=""`
预期：全 PASS（含既有 `test_create_stop_order_clamps_to_market_max_qty` 不受影响）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/base.py gridtrade/exchanges/ccxt_adapter.py tests/exchanges/test_ccxt_adapter.py
git commit -m "feat(exchanges): Instrument.market_max_qty 字段——保险丝覆盖率门的数据面(spec 2026-07-15 §三)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 纯函数 —— `execution/fuse_policy.py`（spec §四、§六）

**Files:**
- Create: `gridtrade/execution/fuse_policy.py`
- Test: `tests/execution/test_fuse_policy.py`

**Interfaces:**
- Produces（Task 3/4 消费，签名固定）:
  - `fuse_worst(cap, gearing, grid_params, min_amount=0.0) -> Optional[float]`——满仓最大持仓量；建不了网 → `None`。
  - `fuse_capped_cap(cap, gearing, grid_params, market_max_qty, *, min_amount=0.0, min_coverage=1.0) -> (float, Optional[float])`——返回 `(cap', coverage)`。
  - `audit_fuse_coverage(universe, prices, max_qtys, cap, gearing) -> dict`——返回 `{'need': float, 'total': int, 'short': [(symbol, coverage)…]}`（`short` 按覆盖率升序）。
- Consumes: `gridtrade.core.grid_engine.grid_order_info`。

- [ ] **Step 1: 写失败测试**

创建 `tests/execution/test_fuse_policy.py`：

```python
"""保险丝覆盖率策略纯函数（spec 2026-07-15 §四/§六）。
worst = 每笔数量 × grid_count（与 executor.open 同源：max_rate=1.0）；
coverage = maxQty/worst；不足额时降 cap 到刚好足额（coverage'=1.0）。"""
import pytest

from gridtrade.execution.fuse_policy import (audit_fuse_coverage, fuse_capped_cap,
                                             fuse_worst)

GEARING = 3.4
GP = {'low_price': 100.0, 'high_price': 120.0, 'grid_count': 20,
      'stop_low_price': 95.0, 'stop_high_price': 125.0}


def test_worst_matches_executor_formula():
    # 与 executor.open 同源：grid_order_info(max_rate=1.0) 的每笔数量 × grid_count
    from gridtrade.core.grid_engine import grid_order_info
    gi = grid_order_info(100.0, GEARING, GP['low_price'], GP['high_price'],
                         GP['grid_count'], GP['stop_low_price'], GP['stop_high_price'],
                         min_amount=0.0, max_rate=1.0)
    assert fuse_worst(100.0, GEARING, GP) == pytest.approx(
        float(gi['每笔数量']) * GP['grid_count'])


def test_full_coverage_leaves_cap_untouched():
    # maxQty 远大于 worst → 足额，cap 原样，coverage>1
    w = fuse_worst(100.0, GEARING, GP)
    cap2, cov = fuse_capped_cap(100.0, GEARING, GP, w * 10)
    assert cap2 == 100.0 and cov == pytest.approx(10.0)


def test_shortfall_caps_down_to_exactly_full():
    # maxQty = worst 的一半 → 降 cap 到足额（无取整时 worst' 恰 == maxQty，不多缩一分仓位）
    w = fuse_worst(100.0, GEARING, GP)
    mx = w / 2.0
    cap2, cov = fuse_capped_cap(100.0, GEARING, GP, mx)
    assert cov == pytest.approx(0.5)              # 干预前的覆盖率
    assert cap2 == pytest.approx(50.0)            # 线性缩放（min_amount=0 → 无取整）
    w2 = fuse_worst(cap2, GEARING, GP)
    assert w2 <= mx * (1 + 1e-9)                  # 足额（护全额）
    assert w2 == pytest.approx(mx)                # 且刚好


def test_capdown_never_raises_on_lot_step_boundary():
    # 取整阶梯回归（评审实证 2026-07-15）：覆盖率 99% + min_amount=0.001 时，
    # 旧算法 cap×coverage 会让每笔数量落同一档不变 → worst' 不降 → 断言抛异常。
    # 新算法（未取整 worst 求解）必须既不抛异常、又真的足额。
    gp = dict(GP)
    w = fuse_worst(10.0, 1.0, gp, min_amount=0.001)
    mx = w * 0.99                                  # 最常见的"差一点"场景
    cap2, cov = fuse_capped_cap(10.0, 1.0, gp, mx, min_amount=0.001)   # 不得抛
    assert cov == pytest.approx(0.99)
    w2 = fuse_worst(cap2, 1.0, gp, min_amount=0.001)
    assert w2 is not None and w2 <= mx * (1 + 1e-9)     # 取整后仍足额


def test_capdown_never_increases_cap():
    # 护栏绝不放大仓位（评审实证 2026-07-15）：min_coverage>1 时已足额币（coverage∈[1,mc)）
    # 也会进干预分支——必须 clamp 成不动，否则 cap 会被放大到 worst==maxQty。
    w = fuse_worst(100.0, GEARING, GP)
    for mc, mx_mult in ((1.2, 1.10), (2.0, 1.90)):      # 已足额（coverage>1）却低于 mc
        cap2, cov = fuse_capped_cap(100.0, GEARING, GP, w * mx_mult, min_coverage=mc)
        assert cov == pytest.approx(mx_mult)
        assert cap2 <= 100.0                            # 只降不升（此处应恰为不动）


def test_unknown_max_qty_fails_open():
    # maxQty 未知（0/None）→ 不干预（交易所自会校验）
    for mx in (0.0, None):
        cap2, cov = fuse_capped_cap(100.0, GEARING, GP, mx)
        assert cap2 == 100.0 and cov is None


def test_min_coverage_zero_disables_intervention_but_still_reports():
    # 停用开关：只算 coverage 供审计，不降 cap
    w = fuse_worst(100.0, GEARING, GP)
    cap2, cov = fuse_capped_cap(100.0, GEARING, GP, w / 2.0, min_coverage=0.0)
    assert cap2 == 100.0 and cov == pytest.approx(0.5)


def test_min_coverage_is_trigger_threshold_not_target():
    # min_coverage 只是触发阈值：0.8 容忍 0.9（不动），但 0.5 触发后降到足额（非 0.8）
    w = fuse_worst(100.0, GEARING, GP)
    cap_a, _ = fuse_capped_cap(100.0, GEARING, GP, w * 0.9, min_coverage=0.8)
    assert cap_a == 100.0                                   # 0.9 ≥ 0.8 → 容忍
    cap_b, _ = fuse_capped_cap(100.0, GEARING, GP, w * 0.5, min_coverage=0.8)
    assert fuse_worst(cap_b, GEARING, GP) == pytest.approx(w * 0.5)   # 降到足额，非 0.8


def test_min_amount_rounding_still_within_max_qty():
    # min_amount 向下取整只减不增 → 降档后仍达标（不得因取整反超 maxQty）
    w = fuse_worst(100.0, GEARING, GP, min_amount=0.001)
    mx = w * 0.37
    cap2, _ = fuse_capped_cap(100.0, GEARING, GP, mx, min_amount=0.001)
    w2 = fuse_worst(cap2, GEARING, GP, min_amount=0.001)
    assert w2 is not None and w2 <= mx * (1 + 1e-9)


def test_ungriddable_cap_fails_open():
    # cap 太低 → grid_order_info 返 None → 不干预（交给 MinNotionalGate 拒）
    assert fuse_worst(0.0, GEARING, GP) is None
    cap2, cov = fuse_capped_cap(0.0, GEARING, GP, 1.0)
    assert cap2 == 0.0 and cov is None


def test_audit_lists_shortfall_sorted_and_skips_unknown():
    # 票池审计（近似口径）：满仓名义 = cap×gearing；足额 ⟺ maxQty×price ≥ 满仓名义
    au = audit_fuse_coverage(
        ['A/USDT:USDT', 'B/USDT:USDT', 'C/USDT:USDT', 'D/USDT:USDT'],
        prices={'A/USDT:USDT': 1.0, 'B/USDT:USDT': 1.0, 'C/USDT:USDT': 1.0},
        max_qtys={'A/USDT:USDT': 100.0, 'B/USDT:USDT': 50.0, 'C/USDT:USDT': 340.0,
                  'D/USDT:USDT': 999.0},
        cap=100.0, gearing=GEARING)                       # 满仓名义 = 340
    assert au['need'] == pytest.approx(340.0)
    assert au['total'] == 3                               # D 缺价 → 跳过（不参与审计）
    assert [s for s, _ in au['short']] == ['B/USDT:USDT', 'A/USDT:USDT']  # 覆盖率升序
    assert au['short'][0][1] == pytest.approx(50.0 / 340.0)


def test_audit_all_covered():
    au = audit_fuse_coverage(['A/USDT:USDT'], prices={'A/USDT:USDT': 1.0},
                             max_qtys={'A/USDT:USDT': 10_000.0},
                             cap=100.0, gearing=GEARING)
    assert au['short'] == [] and au['total'] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_fuse_policy.py -q -o addopts=""`
预期：FAIL — `ModuleNotFoundError: No module named 'gridtrade.execution.fuse_policy'`。

- [ ] **Step 3: 实现 `gridtrade/execution/fuse_policy.py`**

```python
"""保险丝覆盖率策略（纯函数；spec 2026-07-15-fuse-coverage-guard）。

保险丝 = 每格两张 reduce-only STOP_MARKET，数量 worst=满仓最大持仓。币安
MARKET_LOT_SIZE.maxQty 限制单笔市价单数量：worst>maxQty 时下单被 -4005 拒（ed4616e 起
适配器封顶到 maxQty，代价是超出部分无原生硬保护）。本模块给出"开仓前把 cap 降到丝能护
全额"的口径。

主网现状（2026-07-15 实测）：票池最小市价名义上限 $30,570 > 满仓名义额 ⇒ 恒不触发
（临界权益 ≈$36,684）；权益长大后自动接管。demo 的 maxQty 比主网小 3-1200 倍 ⇒ 会真实触发。
"""
from gridtrade.core.grid_engine import grid_order_info


def fuse_worst(cap, gearing, grid_params, min_amount=0.0):
    """满仓最大持仓量 worst = 每笔数量 × grid_count。
    口径与 executor.open 同源（grid_executor.py:81-84，**max_rate=1.0**，非回测的 0.68）。
    cap 太低建不了网 → None（调用方 fail-open）。"""
    gp = grid_params
    gi = grid_order_info(float(cap), float(gearing), gp['low_price'], gp['high_price'],
                         int(gp['grid_count']), gp['stop_low_price'], gp['stop_high_price'],
                         min_amount=float(min_amount), max_rate=1.0)
    if gi is None:
        return None
    return float(gi['每笔数量']) * int(gp['grid_count'])


def fuse_capped_cap(cap, gearing, grid_params, market_max_qty, *,
                    min_amount=0.0, min_coverage=1.0):
    """返回 (cap', coverage)。coverage = maxQty/worst（1.0=足额；None=未知/不可算）。

    min_coverage 是**干预触发阈值**——一旦干预就降到足额（coverage'=1.0），不存在"降到
    80% 就收手"的中间态（那既不省仓位又不护全额）。min_coverage<=0 = 停用（仅算 coverage
    供审计）。

    fail-open：maxQty 未知（<=0）或建不了网 → 原样返回、coverage=None，绝不因限额表读不到
    而干预（交易所自会校验；MinNotionalGate 兜底拒建不了网的 cap）。"""
    cap = float(cap)
    mx = float(market_max_qty or 0.0)
    if mx <= 0:
        return cap, None
    worst = fuse_worst(cap, gearing, grid_params, min_amount)
    if worst is None or worst <= 0:
        return cap, None
    coverage = mx / worst
    if float(min_coverage) <= 0 or coverage >= float(min_coverage):
        return cap, coverage
    # 降档用**未取整** worst 求解（评审实证 2026-07-15）：min_amount=0 时 grid_order_info
    # 跳过向下取整 ⇒ worst_raw 对 cap 严格线性 ⇒ cap'=cap×maxQty/worst_raw 使
    # worst_raw(cap')=maxQty，而真实（取整后）worst'(cap') ≤ worst_raw(cap') = maxQty 必然成立。
    # 【勿改回 cap×coverage】：coverage 基于取整后 worst，取整是阶梯函数——cap 降 1% 时每笔
    # 数量可能落同一档不变 → worst' 不降 > maxQty → 断言抛异常（覆盖率 99% 实测必炸）。
    worst_raw = fuse_worst(cap, gearing, grid_params, 0.0)
    if worst_raw is None or worst_raw <= 0:
        return cap, coverage       # 理论不可达（worst 已算出）；防御性 fail-open
    # 只降不升（评审实证 2026-07-15）：min_coverage>1 时 coverage∈[1, min_coverage) 的"已足额"
    # 币也会进到这里，若不 clamp，cap 会被**放大**到 worst==maxQty——名为降档的护栏变成仓位
    # 放大器（运维把 FUSE_MIN_COVERAGE=1.2 理解成"留 20% 余量"即触发）。护栏绝不放大仓位。
    cap2 = min(cap, cap * (mx / worst_raw))
    w2 = fuse_worst(cap2, gearing, grid_params, min_amount)
    if w2 is not None and w2 > mx * (1 + 1e-9):   # 守卫：防未来 grid_order_info 改动破坏线性
        raise AssertionError('fuse cap-down 失效: worst=%.8g > maxQty=%.8g' % (w2, mx))
    return cap2, coverage


def audit_fuse_coverage(universe, prices, max_qtys, cap, gearing):
    """票池级保险丝覆盖审计（近似口径，spec §一）：满仓名义额 ≈ cap×gearing；
    某币足额 ⟺ maxQty×price ≥ 满仓名义额。

    返回 {'need': 满仓名义额, 'total': 参与审计的币数, 'short': [(symbol, coverage)…]}
    （short 按覆盖率升序）。缺价/缺 maxQty 的币跳过（不参与审计，不误报）。
    用途：让"逼近临界权益"提前可见——报出不足额币即"实盘几何开始偏离回测"的信号（§七）。

    ⚠ **近似口径、非保守（评审实证 2026-07-15）**：选币轮拿不到 per-symbol 网格几何
    （low/high/grid_count 由 ATR 现算），故用"现价"代替网格价梯的均价。现价落在网格带上沿时
    会**高估**覆盖率几个百分点 → 边界处可能漏报（真实 99% 却算作足额）。这只影响"预警早晚"，
    **不影响保护**：真正的护栏是 FuseCoverageGate 开仓前用 grid_order_info 的精确计算。"""
    need = float(cap) * float(gearing)
    short = []
    total = 0
    for s in universe:
        mx = float((max_qtys or {}).get(s) or 0.0)
        px = float((prices or {}).get(s) or 0.0)
        if mx <= 0 or px <= 0 or need <= 0:
            continue
        total += 1
        cov = mx * px / need
        if cov < 1.0:
            short.append((s, cov))
    short.sort(key=lambda x: x[1])
    return {'need': need, 'total': total, 'short': short}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/execution/test_fuse_policy.py -q -o addopts=""`
预期：10 passed。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/execution/fuse_policy.py tests/execution/test_fuse_policy.py
git commit -m "feat(execution): fuse_policy 纯函数——覆盖率/降档 cap/票池审计,口径与 executor.open 同源(max_rate=1.0,spec 2026-07-15 §四§六)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 门链 —— `FuseCoverageGate` + 配置 + 接线（spec §五、§七）

**Files:**
- Modify: `gridtrade/execution/gates.py`（新增 `FuseCoverageGate` 类，置于 `MinNotionalGate` 之前）
- Modify: `gridtrade/config.py`（`DeployConfig.fuse_min_coverage` + `load_deploy_config`）
- Modify: `gridtrade/runtime/factory.py`（门链插入 + 传参）
- Modify: `deploy/fly.toml`、`deploy/fly.prod.toml`（`FUSE_MIN_COVERAGE = "1.0"`）
- Modify: `.env.example`（新键注释）
- Test: `tests/execution/test_gates.py`、`tests/test_config.py`

**Interfaces:**
- Consumes: Task 1 `Instrument.market_max_qty`；Task 2 `fuse_capped_cap`。
- Produces: `FuseCoverageGate(executor, min_coverage, *, adapter=None, log=None)`——不足额时写回 `proposal.cap`；`cap' < executor.cap_min` 时拒。`DeployConfig.fuse_min_coverage: float = 1.0`（env `FUSE_MIN_COVERAGE`）。

- [ ] **Step 1: 写失败测试**

在 `tests/execution/test_gates.py` 末尾追加（`_Ex` 桩带 `cap_min`，与该文件既有 MinNotionalGate 用例同风格）：

```python
def _fuse_ex(cap=100.0, cap_min=20.0):
    class _Ex:
        gearing = 3.4
        min_amount = 0.0
        def __init__(self):
            self.cap_min = cap_min
        def _resolve_cap(self):
            return cap
    return _Ex()


def _fuse_gp():
    return dict(low_price=100.0, high_price=120.0, grid_count=20,
                stop_low_price=95.0, stop_high_price=125.0)


def _fuse_adapter(max_qty):
    # FakeExchange 只需提供带 market_max_qty 的 Instrument
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange
    return FakeExchange(instruments=[
        Instrument(symbol='BTC/USDT:USDT', tick=0.1, lot=0.001, min_size=0.001,
                   state='live', list_ts=0, min_cost=0.0, market_max_qty=max_qty)])


def test_fuse_gate_caps_down_and_writes_back_proposal_cap():
    # 不足额（maxQty=worst/2）→ 降 cap 到刚好足额，写回 proposal.cap 供后续门/executor 用
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    w = fuse_worst(100.0, 3.4, gp)
    gate = FuseCoverageGate(_fuse_ex(), 1.0, adapter=_fuse_adapter(w / 2.0))
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    res = gate.check(p)
    assert res.passed
    assert p.cap == pytest.approx(50.0)                       # 定稿 cap 写回提议
    assert fuse_worst(p.cap, 3.4, gp) <= (w / 2.0) * (1 + 1e-9)   # 丝护全额


def test_fuse_gate_passes_when_covered():
    # 足额 → 放行且不动 cap（proposal.cap 保持 None，executor 用动态 cap）
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    gate = FuseCoverageGate(_fuse_ex(), 1.0,
                            adapter=_fuse_adapter(fuse_worst(100.0, 3.4, gp) * 10))
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    assert gate.check(p).passed and p.cap is None


def test_fuse_gate_rejects_when_capped_below_cap_min():
    # 降档后 cap' < CAP_MIN → 拒（安全失败，不建死网格）
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    w = fuse_worst(100.0, 3.4, gp)
    gate = FuseCoverageGate(_fuse_ex(cap_min=60.0), 1.0,
                            adapter=_fuse_adapter(w * 0.5))     # cap'=50 < CAP_MIN 60
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    res = gate.check(p)
    assert not res.passed and 'CAP_MIN' in res.reason


def test_fuse_gate_fails_open_on_unknown_max_qty_and_adapter_error():
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    # ① maxQty=0（未知）→ 放行不干预
    gate = FuseCoverageGate(_fuse_ex(), 1.0, adapter=_fuse_adapter(0.0))
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    assert gate.check(p).passed and p.cap is None
    # ② list_instruments 抛异常 → 空映射 fail-open（绝不因限额表读不到而拒单）
    class _Boom:
        def list_instruments(self):
            raise RuntimeError('limits unavailable')
    logs = []
    gate2 = FuseCoverageGate(_fuse_ex(), 1.0, adapter=_Boom(), log=logs.append)
    gate2.begin_batch()
    p2 = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    assert gate2.check(p2).passed and p2.cap is None
    assert any('FuseCoverageGate' in m for m in logs)


def test_fuse_gate_disabled_when_min_coverage_zero():
    # 停用开关（紧急回退）：不足额也放行不动 cap
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    gate = FuseCoverageGate(_fuse_ex(), 0.0,
                            adapter=_fuse_adapter(fuse_worst(100.0, 3.4, gp) * 0.1))
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    assert gate.check(p).passed and p.cap is None


def test_fuse_gate_then_min_notional_gate_rejects_unviable_capdown():
    # DRY 分工验证：FuseCoverage 只降 cap，"降后每笔名义额不够"由 MinNotionalGate 自然拒
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import (FuseCoverageGate, GateChain,
                                           MinNotionalGate, GridProposal)
    gp = _fuse_gp()
    w = fuse_worst(100.0, 3.4, gp)
    ex = _fuse_ex(cap_min=1.0)                      # CAP_MIN 极低 → 不在 FuseGate 被拒
    adapter = _fuse_adapter(w * 0.02)               # 覆盖率 2% → cap'≈2
    chain = GateChain([FuseCoverageGate(ex, 1.0, adapter=adapter),
                       MinNotionalGate(ex, 5.0, adapter=adapter)])
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    kept = chain.filter([p])
    assert kept == []                                # 被 MinNotionalGate 拒（非 FuseGate）
    assert chain.evaluate(p).gate == 'MinNotionalGate'
```

在 `tests/test_config.py` 末尾追加：

```python
def test_fuse_min_coverage_parsed():
    # 保险丝覆盖率门槛（spec 2026-07-15）；默认 1.0=必须足额，0=仅审计不干预
    from gridtrade.config import load_deploy_config
    assert load_deploy_config({}).fuse_min_coverage == 1.0
    assert load_deploy_config({'FUSE_MIN_COVERAGE': '0'}).fuse_min_coverage == 0.0


def test_fuse_min_coverage_above_one_rejected():
    # >1 无意义且是语义陷阱（"留 20% 余量"的自然误读会把已足额币白缩仓）→ boot 直接报错
    import pytest
    from gridtrade.config import load_deploy_config
    with pytest.raises(RuntimeError):
        load_deploy_config({'FUSE_MIN_COVERAGE': '1.2'})
```

**同时**改 `tests/runtime/test_factory.py:31` 的既有用例——它钉死了"四门、Margin 最后"，现为五门（**改名 + 改断言**，保持其原意图：门链形状与顺序被守卫）：

```python
def test_build_runtime_gate_chain_has_five_gates_fuse_before_cap_consumers():
    # FuseCoverageGate 必须在"吃 cap"的门（RiskBudget/MinNotional/Margin）之前：
    # 它写回 proposal.cap，后续门须看到定稿 cap（spec 2026-07-15 §五）
    from gridtrade.execution.gates import (FuseCoverageGate, MarginGate,
                                           MaxConcurrentGate, MinNotionalGate,
                                           RiskBudgetGate)
    rt = _rt()                      # 复用该文件既有 _rt() 夹具，勿新造
    gates = rt.manager.gates.gates
    assert len(gates) == 5
    assert isinstance(gates[0], MaxConcurrentGate)
    assert isinstance(gates[1], FuseCoverageGate)
    assert isinstance(gates[2], RiskBudgetGate)
    assert isinstance(gates[3], MinNotionalGate)
    assert isinstance(gates[4], MarginGate)
```

> 实现者注：该用例第 31 行起的原体（含 import 行与 `_rt()` 构造方式）先读原文件，**照其既有写法**改断言与函数名——上面给的是目标形态，不是要你换掉它的夹具。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/execution/test_gates.py tests/test_config.py -q -o addopts=""`
预期：新用例 FAIL（`ImportError: cannot import name 'FuseCoverageGate'`；`fuse_min_coverage` 属性不存在）。

- [ ] **Step 3: 实现**

① `gridtrade/execution/gates.py`——在 `class MinNotionalGate` **之前**插入（文件顶部已有 `GridProposal`/`GateResult`/`AdmissionGate`）：

```python
class FuseCoverageGate(AdmissionGate):
    """保险丝覆盖率门（spec 2026-07-15）：保险丝数量 worst=order_num×grid_count 受币安
    MARKET_LOT_SIZE.maxQty 限制——超限被 -4005 拒（ed4616e 起适配器封顶到 maxQty，代价是
    超出部分无原生硬保护，只剩软止损 5s 轮 + 爆仓线）。

    本门在开仓前把 cap 降到"丝能护全额"的水平（保住币、只缩仓）；降到 CAP_MIN 之下才拒
    （安全失败，不建死网格）。**降后"每笔名义额够不够"不在此重复实现**——交给链上紧随其后
    的 MinNotionalGate 用新 cap 自然拒（DRY）。故链序必须是
    FuseCoverage → RiskBudget → MinNotional → Margin：cap 在被任何"吃 cap"的门消费前定稿。

    主网当前恒不触发（票池最小市价名义上限 $30,570 > 满仓名义额；临界权益 ≈$36,684），
    权益长大后自动接管；demo 的 maxQty 比主网小 3-1200 倍故会真实触发。
    min_coverage<=0 = 停用（紧急回退）。begin_batch 刷按币 maxQty 映射；取数失败 fail-open
    （与 MinNotionalGate 同构——绝不因限额表读不到而拒单）。"""

    def __init__(self, executor, min_coverage, *, adapter=None, log=None):
        self.executor = executor
        self.min_coverage = float(min_coverage)
        self.adapter = adapter          # 按币 maxQty 来源（Instrument.market_max_qty）
        self._max_qty = None            # None=未加载；{}=无数据（fail-open 不干预）
        self.log = log

    def begin_batch(self) -> None:
        if self.adapter is None:
            self._max_qty = {}
            return
        try:
            self._max_qty = {i.symbol: float(getattr(i, 'market_max_qty', 0.0) or 0.0)
                             for i in self.adapter.list_instruments()}
        except Exception as exc:        # fail-open：限额表读不到只退化，不拒单
            self._max_qty = {}
            if self.log is not None:
                self.log('[gate] FuseCoverageGate: list_instruments failed %r' % (exc,))

    def check(self, proposal: GridProposal) -> GateResult:
        if self._max_qty is None:       # 未经 begin_batch 的独立 evaluate → 惰性加载一次
            self.begin_batch()
        if self.min_coverage <= 0:      # 停用（紧急回退）
            return GateResult(True, 'FuseCoverageGate')
        from gridtrade.execution.fuse_policy import fuse_capped_cap
        mx = (self._max_qty or {}).get(proposal.symbol, 0.0)
        cap = (proposal.cap if proposal.cap is not None
               else self.executor._resolve_cap())
        cap2, cov = fuse_capped_cap(cap, self.executor.gearing, proposal.grid_params, mx,
                                    min_amount=self.executor.min_amount,
                                    min_coverage=self.min_coverage)
        if cov is None or cap2 >= cap:  # 未知/建不了网/足额 → 放行不干预
            return GateResult(True, 'FuseCoverageGate')
        if cap2 < self.executor.cap_min:
            return GateResult(False, 'FuseCoverageGate',
                              'fuse coverage %.0f%% → cap %.2f->%.2f < CAP_MIN %.2f'
                              % (100.0 * cov, cap, cap2, self.executor.cap_min))
        proposal.cap = cap2             # 定稿 cap：后续门与 executor.open 都 honor
        if self.log is not None:
            self.log('[gate] FuseCoverageGate: %s 丝覆盖 %.0f%% → cap %.2f->%.2f（降档护全额）'
                     % (proposal.symbol, 100.0 * cov, cap, cap2))
        return GateResult(True, 'FuseCoverageGate')
```

② `gridtrade/config.py`——`DeployConfig` 在 `universe_top_volume_pct` 行**之后**追加：

```python
    fuse_min_coverage: float = 1.0  # 保险丝覆盖率门槛（spec 2026-07-15）：<该值即降 cap 护全额；0=停用（仅审计）。合法区间 (0, 1.0]——>1 无意义（覆盖率>1 只是余量，护栏已 clamp 成只降不升）
```

`load_deploy_config` 在 `universe_top_volume_pct=...` 行**之后**追加：

```python
        fuse_min_coverage=_f(env, 'FUSE_MIN_COVERAGE', 1.0),
```

**并在 `load_deploy_config` 开头的退役键守卫之后、`cap = _f(env, 'CAP', 100.0)` 之前**追加合法
区间守卫（沿本仓 fail-fast 惯例；评审+实现者双指出 >1 是语义陷阱：覆盖率>1 只是余量、丝本就
能全平，设 1.2 只会把已足额的币白白缩仓）：

```python
    # 保险丝覆盖率门槛合法区间 (0, 1.0]（spec 2026-07-15）：>1 无意义——coverage>1 只是余量
    # （丝本就能全平最大持仓），设 1.2 这类"留余量"的自然误读只会把已足额的币白缩一个 lot 步。
    # 0/负 = 停用（仅审计）。禁静默 clamp：配置错了要响亮。
    _fmc = _f(env, 'FUSE_MIN_COVERAGE', 1.0)
    if _fmc > 1.0:
        raise RuntimeError('FUSE_MIN_COVERAGE=%s 无效：合法区间 (0, 1.0]（>1 无意义，'
                           '覆盖率>1 只是余量；0=停用仅审计）' % _fmc)
```

③ `gridtrade/runtime/factory.py`——import 增补 `FuseCoverageGate`，门链按新序：

```python
from gridtrade.execution.gates import (FuseCoverageGate, GateChain, MarginGate,
                                       MaxConcurrentGate, MinNotionalGate,
                                       RiskBudgetGate)
```

```python
    gates = GateChain([
        MaxConcurrentGate(executor.grids, config.max_concurrent),
        # cap 定稿必须在"吃 cap"的门（RiskBudget/MinNotional/Margin）之前（spec 2026-07-15 §五）
        FuseCoverageGate(executor, config.fuse_min_coverage, adapter=adapter,
                         log=_flush_log),
        RiskBudgetGate(executor.grids, config.total_budget, config.default_cap),
        MinNotionalGate(executor, config.min_order_notional, adapter=adapter,
                        log=_flush_log),
        MarginGate(adapter, config.default_cap, executor=executor, log=_flush_log),
    ], log=_flush_log)
```

④ `deploy/fly.toml` 与 `deploy/fly.prod.toml`——在 `MIN_ORDER_NOTIONAL` 行之后各加：

```toml
  # 保险丝覆盖率门槛（spec 2026-07-15）：worst>MARKET_LOT_SIZE.maxQty 时降 cap 护全额、
  # 降到 CAP_MIN 之下才拒币。主网当前恒不触发（临界权益 ≈$36.7k）；demo 会真实触发。0=停用（仅审计）。
  FUSE_MIN_COVERAGE = "1.0"
```

⑤ `.env.example`——在 `MIN_ORDER_NOTIONAL` 行之后加：

```bash
# 保险丝覆盖率门槛（spec 2026-07-15）：丝数量超币安 MARKET_LOT_SIZE.maxQty 时降 cap 护全额；
# 0=停用（仅审计不干预）
FUSE_MIN_COVERAGE=1.0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/execution/test_gates.py tests/test_config.py tests/runtime/test_factory.py -q -o addopts=""`
预期：全 PASS。若 `test_factory.py` 断言门链长度/顺序，同步更新为 5 门（新序见上）。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/execution/gates.py gridtrade/config.py gridtrade/runtime/factory.py deploy/fly.toml deploy/fly.prod.toml .env.example tests/execution/test_gates.py tests/test_config.py tests/runtime/test_factory.py
git commit -m "feat(execution,config): FuseCoverageGate 门链——丝不足额降 cap 护全额/CAP_MIN 之下拒币,FUSE_MIN_COVERAGE=1.0 两侧(spec 2026-07-15 §五§七)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 可观测性 —— 封顶日志含覆盖率 + 选币轮审计（spec §六）

**Files:**
- Modify: `gridtrade/exchanges/binance.py`（`create_stop_order` 封顶日志）
- Modify: `gridtrade/runtime/scheduler.py`（`run_scheduler_once` 票池审计）
- Test: `tests/exchanges/test_binance_adapter.py`、`tests/runtime/test_scheduler.py`

**Interfaces:**
- Consumes: Task 1 `Instrument.market_max_qty`；Task 2 `audit_fuse_coverage`。
- Produces: 无新公共接口（纯日志）。

**设计注记（对 spec §六 的落地细化，同意图、更小面）**：spec 写"executor.open 打结构化告警"。实现上**封顶发生在适配器**（`create_stop_order`），它同时握有 `size` 与 `mx`——在此处打日志无需给 executor 新增端口方法，且**所有路径**（自动/手动 `OPEN_GRID`/`reconcile_fuses` 重挂）天然覆盖。故把告警落在适配器既有 clamp 分支（增强为含覆盖率+cloid），executor 不动。

- [ ] **Step 1: 写失败测试**

在 `tests/exchanges/test_binance_adapter.py` 末尾追加：

```python
def test_stop_order_clamp_log_reports_coverage(capsys):
    # 封顶时须打出覆盖率与 cloid（spec 2026-07-15 §六：不足额必须响亮可见）
    c = FakeBinanceClient()
    a = _binance(c)
    a.create_stop_order('BTC/USDT:USDT', 'sell', 480.0, 95.0, client_oid='9:fuse:low')
    out = capsys.readouterr().out
    assert '封顶' in out and '25%' in out and '9:fuse:low' in out   # 120/480 = 25%
```

在 `tests/runtime/test_scheduler.py` 末尾追加（**复用该文件既有的 `_rt()` 夹具**——它经
`build_runtime(load_deploy_config(env={'EXCHANGE': 'fake'}))` 造出真 Runtime，`rt.adapter`
是 `ResilientAdapter(FakeExchange)`，`rt.executor` 是真 `GridExecutor`；勿新造桩）：

```python
def _seed_universe(rt, symbol, price, max_qty):
    """给 fake 交易所塞一个带 market_max_qty 的在市币（ResilientAdapter 内层）。"""
    from gridtrade.exchanges.base import Instrument
    inner = rt.adapter._inner
    inner._instruments = [Instrument(symbol, 0.1, 0.1, 0.1, 'live', 0,
                                     min_cost=0.0, market_max_qty=max_qty)]
    inner.set_price(symbol, price)
    return inner


def test_scheduler_audits_fuse_coverage_shortfall(capsys):
    # 选币轮审计：不足额币须报出（含最差币与覆盖率）+ 偏离回测提示（spec 2026-07-15 §六）
    # fake 权益 1e6 → cap=CAP_MAX=100000 → 满仓名义=340000；maxQty×px=1 → 覆盖率≈0
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt()
    _seed_universe(rt, 'LOW/USDT:USDT', price=1.0, max_qty=1.0)
    run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0,
                       fetch_candles=lambda *a, **k: {})     # 空 K 线 → 不开仓
    out = capsys.readouterr().out
    assert '保险丝不足额' in out and 'LOW/USDT:USDT' in out


def test_scheduler_audits_fuse_coverage_ok(capsys):
    # 全足额 → 报 OK（审计恒有输出，便于确认审计本身在跑）
    from gridtrade.runtime.scheduler import run_scheduler_once
    rt = _rt()
    _seed_universe(rt, 'HI/USDT:USDT', price=1.0, max_qty=1e12)
    run_scheduler_once(rt, now_fn=lambda: 1_750_000_000.0,
                       fetch_candles=lambda *a, **k: {})
    out = capsys.readouterr().out
    assert '保险丝覆盖 OK' in out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/exchanges/test_binance_adapter.py::test_stop_order_clamp_log_reports_coverage tests/runtime/test_scheduler.py -q -o addopts=""`
预期：新用例 FAIL（日志不含覆盖率/审计行）。

- [ ] **Step 3: 实现**

① `gridtrade/exchanges/binance.py`——`create_stop_order` 的 clamp 分支日志替换为：

```python
        mx = self._market_max_qty(symbol)
        if mx is not None and size > mx:
            print('[binance] %s 保险丝封顶: %.8g -> %.8g（MARKET_LOT_SIZE.maxQty），覆盖率 %.0f%%'
                  '——超出部分依赖软止损+爆仓线（cloid=%s；门链应已降 cap 护全额，本日志出现'
                  '即意味着走了手动/fail-open 路径，spec 2026-07-15 §六）'
                  % (symbol, size, mx, 100.0 * mx / size, client_oid), flush=True)
            size = mx
```

② `gridtrade/runtime/scheduler.py`——在 `run_scheduler_once` 里 `universe = resolve_live_universe(...)` 之后、票池后续处理之前插入：

```python
    # 保险丝覆盖审计（spec 2026-07-15 §六）：limits 复用 ccxt 缓存 markets（零权重）；
    # 价格走 fetch_prices_all（币安全市场 ticker/price，权重 2/轮，选币轮每小时一次 → 可忽略）。
    # 报出不足额币 = 权益已跨临界（≈$36.7k）→ 门链开始降 cap，且实盘几何开始偏离回测（§七）。
    try:
        from gridtrade.execution.fuse_policy import audit_fuse_coverage
        _mq = {i.symbol: float(getattr(i, 'market_max_qty', 0.0) or 0.0)
               for i in rt.adapter.list_instruments()}
        _au = audit_fuse_coverage(universe, rt.adapter.fetch_prices_all(universe), _mq,
                                  rt.executor._resolve_cap(), rt.executor.gearing)
        if _au['short']:
            print('[audit] 保险丝不足额 %d/%d 币（最差 %s %.0f%%）：门链将降 cap 护全额；'
                  '实盘几何已偏离回测（spec 2026-07-15 §七）'
                  % (len(_au['short']), _au['total'], _au['short'][0][0],
                     100.0 * _au['short'][0][1]), flush=True)
        else:
            print('[audit] 保险丝覆盖 OK：票池 %d 币全足额（满仓名义 $%.0f）'
                  % (_au['total'], _au['need']), flush=True)
    except Exception as exc:      # 审计失败绝不阻断选币轮
        print('[audit] 保险丝覆盖审计跳过: %r' % (exc,), flush=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/exchanges/ tests/runtime/ -q -o addopts=""`
预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add gridtrade/exchanges/binance.py gridtrade/runtime/scheduler.py tests/exchanges/test_binance_adapter.py tests/runtime/test_scheduler.py
git commit -m "feat(observability): 封顶日志含覆盖率+选币轮票池审计——让"逼近临界权益/偏离回测"提前可见(spec 2026-07-15 §六)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 全量验证收尾

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/python -m pytest -q -o addopts=""`
预期：全 PASS（基线 811 passed / 2 skipped + 本计划新增用例；skip 为 Postgres 门控，正常）。

- [ ] **Step 2: golden parity（引擎/核心未动的证明）**

Run: `.venv/bin/python -m pytest tests/golden/ tests/core/ -q -o addopts=""`
预期：全 PASS——本计划**未改** `core/`（`grid_order_info` 只被读、未被改），回测几何逐位不变。

- [ ] **Step 3: 口径一致性自查（人工 grep）**

```bash
grep -rn "max_rate=" gridtrade/execution/fuse_policy.py gridtrade/execution/gates.py gridtrade/execution/grid_executor.py
```
预期：`fuse_policy.py` / `gates.py`（MinNotionalGate）/ `grid_executor.py:84` 三处**都是 `max_rate=1.0`**（同源口径；回测的 0.68 只在 `backtest/` 出现）。

- [ ] **Step 4: Commit（若扫尾有改动）**

```bash
git add -A
git commit -m "chore: 保险丝覆盖率保障收尾——全量+golden 绿,口径同源自查(spec 2026-07-15)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 未尽事项（明确不在本计划内）

- **不部署**：部署由运维会话手动做（须避开整点 HH:00–HH:12，防与 scheduler 换仓撞车）。
- **回测不建模封顶**：主网不足额集合当前为空 ⇒ 两侧口径一致（spec §七）。一旦 Task 4 的审计日志报出不足额币，即为"实盘几何开始偏离回测"的信号，届时须另立项在回测建模（历史 maxQty 无归档，Vision 只有 K 线）。
- **多张丝分摊（B）/ `closePosition=true`（F）**：备选闸门，触发条件与阻断条件见 spec §八。
