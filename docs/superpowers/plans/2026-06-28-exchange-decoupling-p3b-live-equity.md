# 交易所解耦重构 P3b 实现计划（实盘增量记账 LiveEquity）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `gridtrade/execution/live_equity.py` 的 `LiveEquity`：实盘网格的增量记账。把累计成交流水重建为 trade_df + 一根当前 mark 价的合成 K线，**复用 `core.grid_engine.cal_equity_curve`** 算 net_value/持仓/已实现/手续费，资金费单独累计后扣除 —— 从构造上保证与回测引擎逐值同源（零漂移）。

**Architecture:** 单一职责的记账类。`record_fill` 累积成交（含 last_touch 链）；`snapshot(mark_price)` 用一根"严格晚于最后成交时间"的合成 1m K线（close=mark）调用 `cal_equity_curve`，取末行 net_value（已被原型验证 == 完整路径引擎末值）；`add_funding` 累计资金费、`snapshot` 从 net_value 扣 `funding_paid/cap`；`replay` 供 P3c reconciler 重建。不直接判止损（由监控层组合 `snapshot` + `AccountingRepository.bump_peak` + `core.stop_rules.evaluate_exit`）。

**Tech Stack:** Python 3.9、pandas 1.3.5、numpy 1.22.4、pytest。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（记账口径、引擎复用细节、本计划未写清的地方），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；pandas==1.3.5；numpy==1.22.4。
- `gridtrade/execution/` 可以 import `gridtrade/core/`（引擎/规则），但**不得 import 交易所库（ccxt）或 `gridtrade/state/`**（LiveEquity 是纯记账，状态持久化由调用方负责）。
- LiveEquity 必须通过复用 `cal_equity_curve` 保证记账与回测同源：`snapshot` 的 net_value 必须等于"同一组成交 + 完整逐 bar 路径（末 bar close=mark）"喂 `cal_equity_curve` 得到的末行 net_value（容差 1e-9）。**这是核心验收点。**
- 成交方向：`'buy'→order_dir=+1.0`，`'sell'→order_dir=-1.0`；`last_touch` = 上一笔成交的 touch（首笔用 entry_price，无 entry 则用本笔 price）。这与 `core.grid_engine.get_trade_info` 的 trade_df 口径一致。
- 资金费符号：`add_funding(amount)` 中 amount>0 表示**支付**资金费（净值下降）；`snapshot` 中 `net_value = 引擎net_value - funding_paid/cap`。
- 合成 mark K线时间戳必须**严格晚于**所有成交时间（用 `last_ts + 60000` ms），避免与成交行在 `cal_equity_curve` 的 outer merge 上时间戳碰撞。
- 不修改 `account_0/`、`backtest/`、`gridtrade/{core,exchanges,state}/`。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。

---

## 文件结构（本计划新建）

```
gridtrade/execution/
  __init__.py
  live_equity.py     # LiveEquity（record_fill / snapshot / add_funding / replay）
tests/execution/
  __init__.py
  test_live_equity.py
```

---

### Task 1: LiveEquity 核心（record_fill + snapshot，复用引擎，零漂移 parity）

**Files:**
- Create: `gridtrade/execution/__init__.py`, `gridtrade/execution/live_equity.py`
- Create: `tests/execution/__init__.py`, `tests/execution/test_live_equity.py`

**Interfaces:**
- Consumes: `gridtrade.core.grid_engine.cal_equity_curve`
- Produces: `gridtrade.execution.live_equity.LiveEquity`：
  - `__init__(self, cap, fee=0.0002, c_rate_taker=0.0005, entry_price=None)`
  - `record_fill(self, price, side, size, ts_ms)`：累积一笔成交（side ∈ {'buy','sell'}，否则 ValueError）。
  - `snapshot(self, mark_price) -> dict`：返回 `{net_value, pnl_ratio, net_position, avg_price, realized_pnl, fee_paid, funding_paid}`。无成交时返回零值快照（net_value=1.0）。
  - 属性 `funding_paid`（本任务恒为 0.0，Task 2 起可变）。

- [ ] **Step 1: 写测试**

Create `tests/execution/__init__.py`（空）。

Create `tests/execution/test_live_equity.py`:

```python
import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import cal_equity_curve


CAP = 1000.0
FEE = 0.0002
CRATE = 0.0005


def _le(entry=100.0):
    from gridtrade.execution.live_equity import LiveEquity
    return LiveEquity(CAP, fee=FEE, c_rate_taker=CRATE, entry_price=entry)


# 一组确定性成交（分钟, 价, 方向）；价为网格线
FILLS = [(1, 99.0, 'buy'), (2, 98.0, 'buy'), (3, 99.0, 'sell'), (4, 100.0, 'sell')]


def _truth_net_value(fills, entry, final_price):
    """真值：把同一组成交 + 完整逐 bar 路径喂 cal_equity_curve，取末行 net_value。"""
    rows = []
    last = entry
    for ts, p, side in fills:
        rows.append({'candle_begin_time': pd.to_datetime(ts * 60_000, unit='ms'),
                     'last_touch': float(last), 'touch': float(p),
                     'order_dir': 1.0 if side == 'buy' else -1.0, 'order_num': 0.5})
        last = p
    trade_df = pd.DataFrame(rows)
    # 注意：本真值用固定 order_num=0.5，测试里 record_fill 也用 size=0.5
    n = fills[-1][0] + 2
    tbars = pd.date_range(pd.to_datetime(0, unit='ms'), periods=n, freq='1min')
    closes = []
    fmap = {ts: p for ts, p, _ in fills}
    cur = entry
    for i in range(n):
        cur = fmap.get(i, cur)
        closes.append(cur)
    closes[-1] = final_price
    candle = pd.DataFrame({'candle_begin_time': tbars, 'open': closes, 'high': closes,
                           'low': closes, 'close': closes, 'symbol': 'X'})
    eq = cal_equity_curve(candle, trade_df.copy(), FEE, CAP, CRATE, funding_df=None)
    return float(eq['net_value'].iloc[-1])


def test_empty_snapshot_is_unit():
    snap = _le().snapshot(100.0)
    assert snap['net_value'] == 1.0 and snap['pnl_ratio'] == 0.0
    assert snap['net_position'] == 0.0 and snap['realized_pnl'] == 0.0


def test_snapshot_matches_full_path_engine():
    le = _le(entry=100.0)
    for ts, p, side in FILLS:
        le.record_fill(p, side, 0.5, ts * 60_000)
    final_price = 100.5
    snap = le.snapshot(final_price)
    truth = _truth_net_value(FILLS, 100.0, final_price)
    assert abs(snap['net_value'] - truth) < 1e-9, f"{snap['net_value']} vs {truth}"
    # 全平后净持仓应为 0，已实现 = 两个格子收益 = 2 × gap(1.0) × 0.5 = 1.0
    assert abs(snap['net_position']) < 1e-9
    assert abs(snap['realized_pnl'] - 1.0) < 1e-9


def test_open_position_marks_to_mark_price():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000)   # 持多 0.5 @ 99
    snap = le.snapshot(101.0)                   # mark 101
    assert abs(snap['net_position'] - 0.5) < 1e-9
    assert abs(snap['avg_price'] - 99.0) < 1e-9
    truth = _truth_net_value([(1, 99.0, 'buy')], 100.0, 101.0)
    assert abs(snap['net_value'] - truth) < 1e-9


def test_neutral_init_base_inventory():
    # 复刻 OKX 中性网格开网底仓：entry 上方若干格在 entry 价预置多头；随后上涨卖出兑现
    le = _le(entry=100.0)
    for i in range(3):                          # 3 笔底仓买入 @ entry
        le.record_fill(100.0, 'buy', 0.5, (i + 1) * 60_000)
    le.record_fill(101.0, 'sell', 0.5, 5 * 60_000)   # 上方格卖出
    snap = le.snapshot(101.0)
    assert abs(snap['net_position'] - 1.0) < 1e-9   # 1.5 买 - 0.5 卖 = 1.0
    fills = [(1, 100.0, 'buy'), (2, 100.0, 'buy'), (3, 100.0, 'buy'), (5, 101.0, 'sell')]
    truth = _truth_net_value(fills, 100.0, 101.0)
    assert abs(snap['net_value'] - truth) < 1e-9


def test_bad_side_raises():
    import pytest
    with pytest.raises(ValueError):
        _le().record_fill(100.0, 'long', 0.5, 60_000)
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_live_equity.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.live_equity`）。

- [ ] **Step 3: 写 live_equity.py**

Create `gridtrade/execution/__init__.py`（空）。

Create `gridtrade/execution/live_equity.py`:

```python
"""LiveEquity：实盘网格增量记账，通过复用 core.grid_engine.cal_equity_curve 与回测同源。
把累计成交流水重建为 trade_df + 一根当前 mark 价的合成 1m K线喂引擎，取末行 net_value；
资金费单独累计（funding_paid），从 net_value 扣除。不直接判止损（由监控层组合）。
"""
from typing import Optional

import pandas as pd

from gridtrade.core.grid_engine import cal_equity_curve


class LiveEquity:
    def __init__(self, cap, fee=0.0002, c_rate_taker=0.0005,
                 entry_price: Optional[float] = None):
        self.cap = float(cap)
        self.fee = float(fee)
        self.c_rate_taker = float(c_rate_taker)
        self.entry_price = None if entry_price is None else float(entry_price)
        self._fills = []      # trade_df 行：candle_begin_time/last_touch/touch/order_dir/order_num
        self._last_ts = None  # 最后成交时间（ms）
        self.funding_paid = 0.0

    def record_fill(self, price, side, size, ts_ms):
        if side not in ('buy', 'sell'):
            raise ValueError("side must be 'buy' or 'sell'")
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

    def snapshot(self, mark_price) -> dict:
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
        net_value = float(last['net_value']) - self.funding_paid / self.cap
        return {'net_value': net_value, 'pnl_ratio': net_value - 1.0,
                'net_position': float(last['hold_num']), 'avg_price': float(last['avg_price']),
                'realized_pnl': float(last['real_profit']), 'fee_paid': float(last['fee']),
                'funding_paid': self.funding_paid}
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_live_equity.py -v`
Expected: PASS（5 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/__init__.py gridtrade/execution/live_equity.py tests/execution/__init__.py tests/execution/test_live_equity.py
git commit -m "feat(execution): LiveEquity incremental accounting (reuses cal_equity_curve)"
```

---

### Task 2: 资金费累计 add_funding + reconciler 重建 replay + 全套回归

**Files:**
- Modify: `gridtrade/execution/live_equity.py`
- Modify: `tests/execution/test_live_equity.py`

**Interfaces:**
- Produces（在 LiveEquity 上新增）：
  - `add_funding(self, amount)`：`funding_paid += float(amount)`（amount>0 表示支付）。
  - `replay(self, fills) -> LiveEquity`：`fills` 为可迭代的 `(price, side, size, ts_ms)`，逐笔 `record_fill`，返回自身（供 reconciler 重建）。

- [ ] **Step 1: 追加测试**

在 `tests/execution/test_live_equity.py` 末尾追加：

```python
def test_add_funding_reduces_net_value():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000)
    before = le.snapshot(101.0)['net_value']
    le.add_funding(5.0)                       # 支付 5 USDT 资金费
    after = le.snapshot(101.0)
    assert abs((before - after['net_value']) - 5.0 / CAP) < 1e-12
    assert after['funding_paid'] == 5.0


def test_replay_matches_incremental():
    fills = [(99.0, 'buy', 0.5, 60_000), (98.0, 'buy', 0.5, 120_000),
             (99.0, 'sell', 0.5, 180_000)]
    inc = _le(entry=100.0)
    for price, side, size, ts in fills:
        inc.record_fill(price, side, size, ts)
    rep = _le(entry=100.0).replay(fills)
    a, b = inc.snapshot(100.0), rep.snapshot(100.0)
    assert abs(a['net_value'] - b['net_value']) < 1e-12
    assert abs(a['net_position'] - b['net_position']) < 1e-12
    assert abs(a['realized_pnl'] - b['realized_pnl']) < 1e-12
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_live_equity.py -k "funding or replay" -v`
Expected: FAIL（`AttributeError: 'LiveEquity' object has no attribute 'add_funding'` / `replay`）。

- [ ] **Step 3: 在 live_equity.py 新增方法**

在 `LiveEquity` 类内（`snapshot` 之前或之后）新增：

```python
    def add_funding(self, amount):
        self.funding_paid += float(amount)

    def replay(self, fills) -> 'LiveEquity':
        """fills: 可迭代的 (price, side, size, ts_ms)。供 reconciler 从持久化成交重建。"""
        for price, side, size, ts_ms in fills:
            self.record_fill(price, side, size, ts_ms)
        return self
```

- [ ] **Step 4: 运行确认通过 + 全套回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_live_equity.py -v`
Expected: PASS（7 passed）。

Run（全仓回归）: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全 PASS（既有 65 + 本计划新增 7）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/live_equity.py tests/execution/test_live_equity.py
git commit -m "feat(execution): LiveEquity add_funding + replay (reconciler rebuild)"
```

---

## 完成判定（P3b）

- `pytest -q` 全绿：LiveEquity 空快照/持仓盯市/全平已实现/中性底仓/资金费扣减/replay 重建，且 `snapshot` net_value 与"完整路径 cal_equity_curve"逐值同源（1e-9）。
- `gridtrade/execution/` 不 import 交易所库或 `gridtrade/state/`（`grep -rnE "ccxt|hyperliquid|gridtrade\.state" gridtrade/execution` 仅命中注释，无真实 import）。

## 后续（P3c，不在本计划内）

`execution/grid_executor.py`（挂单网格生命周期状态机：开网=grid_order_info 几何+中性底仓市价买+逐线挂限价单[client_oid=grid:line]；补单；平网=撤单+市价 reduce+落库；驱动 ExchangeAdapter+StateStore[grids/grid_orders/grid_accounting]+LiveEquity）+ `execution/reconciler.py`（重启对账：从 state 载意图，拉交易所 open_orders/position/my_trades，diff 补挂/撤孤儿/用 my_trades replay 重建 LiveEquity）+ 监控层组合 snapshot+bump_peak+evaluate_exit。

> P3c 开始前需与用户确认的语义：**实盘资金费记账来源**——是按交易所实际资金费扣款流水累计，还是在每个结算点用 `net_position × mark × funding_rate` 自算？这影响 add_funding 的喂数方式与适配器是否需新增"资金费流水"查询接口。届时提问。
> 另：监控层调用 `core.stop_rules.evaluate_exit` 时，资金费已知为 0 应传 `funding_rate=0.0`（非 None），以镜像生产中 fundingRate 列恒存在的语义（P3a 最终评审记录）。
