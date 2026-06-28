# 交易所解耦重构 P3a 实现计划（中性引擎迁移 + 标量止损规则）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 backtest 的中性网格引擎 `grid_engine.py` 零漂移迁入 `gridtrade/core/grid_engine.py`（实盘+回测同源），并抽出一个**标量退出评估器** `gridtrade/core/stop_rules.evaluate_exit`，与引擎的向量化 `_apply_exit` 逐 bar 等价，供 P3b 的实盘监控按标量 pnl_ratio+峰值判定止盈止损。

**Architecture:** 纯函数迁移 + 等价抽取。`core/grid_engine.py` 是 `backtest/grid_engine.py` 的逐字副本（金标锁定零漂移，backtest 原件保持不动，P5 再统一）。`core/stop_rules.evaluate_exit` 把 `_apply_exit` 的逐 bar 优先级判定提成一个标量函数，用等价测试证明：对任意 net_value 序列，标量逐行扫描得到的首个触发(reason, idx) == `_apply_exit` 的截断结果。

**Tech Stack:** Python 3.9、numpy 1.22.4、pandas 1.3.5、pytest。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（退出优先级语义、引擎数学、本计划未写清的细节），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；numpy==1.22.4；pandas==1.3.5（引擎依赖其行为，如 `resample`、`merge_asof`、`expanding`）。
- `gridtrade/core/` 不得 import 任何交易所库（ccxt 等）。`grid_engine.py` 仅依赖 `datetime/numpy/pandas`。
- `core/grid_engine.py` 必须与 `backtest/grid_engine.py` 逐字一致（金标锁定零漂移）；不改任何公式/列名/中文键名（价格序列/每笔数量/终止最低价/终止最高价）。
- 退出优先级（`_apply_exit` 与 `evaluate_exit` 同序）：① 固定止损 `pnl_ratio < -stop_loss` ② 连续回撤止盈 `(pnl_ratio_max - pnl_ratio) >= max(trailing_floor, trailing_k×pnl_ratio_max)` 且 `pnl_ratio_max > trailing_floor` ③ 资金费率止损 `|funding_rate| > fundingRate_stop_loss` ④ pv主动止损 `pv_spike==1 且 pnl_ratio < -0.015` ⑤ 爆仓 `net_value < margin_rate`。同一 bar 高优先级先命中。
- 不修改 `account_0/`、`backtest/`、`gridtrade/exchanges/`、`gridtrade/state/`、已有的 `gridtrade/core/{factors,selection,grid_params}.py`。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`（沿用既有 venv：pandas 1.3.5 等）。

---

## 文件结构（本计划新建/修改）

```
gridtrade/core/
  grid_engine.py     # 逐字复制 backtest/grid_engine.py（零漂移）
  stop_rules.py      # 标量 evaluate_exit（与 _apply_exit 等价）
tests/golden/
  gen_grid_engine_golden.py   # 一次性：从 backtest/grid_engine.py 生成金标
  grid_engine_golden.json     # grid_order_info + simulate_grid_engine 金标
tests/core/
  test_grid_engine_parity.py  # core 引擎 vs 金标（零漂移）
  test_stop_rules.py          # evaluate_exit vs core._apply_exit 等价
```

---

### Task 1: grid_engine 金标 fixture（来自 backtest 原件）

**Files:**
- Create: `tests/golden/gen_grid_engine_golden.py`
- Create（产物，提交进仓库）: `tests/golden/grid_engine_golden.json`

**Interfaces:**
- Produces: `tests/golden/grid_engine_golden.json`（含 `grid_order_info` 与 `simulate` 两段）；可复用的确定性造数函数 `make_1m_bars(n, seed, start)`（后续 parity 测试 import 它构造相同输入）。

- [ ] **Step 1: 写造数 + 生成脚本**

Create `tests/golden/gen_grid_engine_golden.py`:

```python
"""一次性脚本：用原始 backtest/grid_engine.py 生成网格引擎金标。
运行：TZ=Asia/Shanghai .venv/bin/python tests/golden/gen_grid_engine_golden.py
重构后由 parity 测试用相同输入比对 core 引擎输出（零漂移）。
"""
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
_BT = os.path.join(_ROOT, 'backtest')
if _BT not in sys.path:
    sys.path.insert(0, _BT)


def make_1m_bars(n=600, seed=7, start=100.0):
    """确定性合成 1m OHLCV（含 quote_volume/symbol），用于网格引擎仿真。"""
    rng = np.random.RandomState(seed)
    rets = rng.normal(0, 0.0008, size=n)
    close = start * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0005, size=n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0005, size=n)))
    qv = rng.uniform(1e5, 1e6, size=n)
    t = pd.date_range('2024-03-01', periods=n, freq='1min')
    return pd.DataFrame({
        'candle_begin_time': t, 'open': open_, 'high': high, 'low': low,
        'close': close, 'quote_volume': qv, 'symbol': 'BTC/USDT:USDT',
    })


def main():
    from grid_engine import grid_order_info, simulate_grid_engine  # 原始 backtest 实现

    gi = grid_order_info(1000.0, 5.0, 90.0, 110.0, 40, 88.0, 112.0)
    gi_out = {
        'price_array': [float(x) for x in gi['价格序列']],
        'order_num': float(gi['每笔数量']),
        'stop_low': float(gi['终止最低价']),
        'stop_high': float(gi['终止最高价']),
    }

    bars = make_1m_bars()
    grid_params = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 40,
                   'stop_low_price': 88.0, 'stop_high_price': 112.0}
    stop_cfg = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
                'fundingRate_stop_loss': 0.0015}
    res = simulate_grid_engine(bars, grid_params, cap=1000.0, leverage=5.0,
                               stop_cfg=stop_cfg)
    sim_out = {
        'pnl_ratio': float(res['pnl_ratio']),
        'net_value_final': float(res['net_value_final']),
        'terminated': bool(res['terminated']),
        'exit_reason': res['exit_reason'],
        'blown_up': bool(res['blown_up']),
        'n_trades': int(res['n_trades']),
        'broke': bool(res['broke']),
    }

    out = {'grid_order_info': gi_out, 'simulate': sim_out}
    with open(os.path.join(_HERE, 'grid_engine_golden.json'), 'w') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print('grid_engine golden written; n_trades=%d exit=%s pnl=%.6f'
          % (sim_out['n_trades'], sim_out['exit_reason'], sim_out['pnl_ratio']))


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: 运行生成脚本**

Run: `TZ=Asia/Shanghai .venv/bin/python tests/golden/gen_grid_engine_golden.py`
Expected: 打印 `grid_engine golden written; ...`，生成 `tests/golden/grid_engine_golden.json`。

- [ ] **Step 3: 校验 fixture 内容合理**

Run:
```bash
python3 -c "import json; d=json.load(open('tests/golden/grid_engine_golden.json')); print('price_array len', len(d['grid_order_info']['price_array']), '| order_num', d['grid_order_info']['order_num'], '| sim', d['simulate'])"
```
Expected: `price_array len 41`（grid_count=40 → 41 条线），`order_num > 0`，`sim` 含 pnl_ratio/exit_reason/n_trades 等字段。

- [ ] **Step 4: 提交**

```bash
git add tests/golden/gen_grid_engine_golden.py tests/golden/grid_engine_golden.json
git commit -m "test: generate grid-engine golden fixture from backtest engine"
```

---

### Task 2: 迁移引擎到 core/grid_engine.py（金标 parity）

**Files:**
- Create: `gridtrade/core/grid_engine.py`
- Create: `tests/core/test_grid_engine_parity.py`

**Interfaces:**
- Consumes: `tests/golden/grid_engine_golden.json`、`tests.golden.gen_grid_engine_golden.make_1m_bars`
- Produces: `gridtrade.core.grid_engine`，暴露与 `backtest/grid_engine.py` 完全一致的函数：`grid_order_info`、`trans_candle_to_tick`、`grid_touch_info`、`get_trade_info`、`calc_pv_spike`、`_apply_exit`、`cal_equity_curve`、`simulate_grid_engine`。签名/逻辑逐字一致。

- [ ] **Step 1: 写 parity 失败测试**

Create `tests/core/test_grid_engine_parity.py`:

```python
import json
import os

import numpy as np

from tests.golden.gen_grid_engine_golden import make_1m_bars

_GOLDEN = os.path.join(os.path.dirname(__file__), '..', 'golden', 'grid_engine_golden.json')


def _golden():
    with open(_GOLDEN, encoding='utf-8') as f:
        return json.load(f)


def test_grid_order_info_matches_golden():
    from gridtrade.core.grid_engine import grid_order_info
    g = _golden()['grid_order_info']
    gi = grid_order_info(1000.0, 5.0, 90.0, 110.0, 40, 88.0, 112.0)
    np.testing.assert_allclose([float(x) for x in gi['价格序列']],
                               g['price_array'], rtol=1e-9, atol=1e-12)
    assert abs(float(gi['每笔数量']) - g['order_num']) < 1e-9
    assert float(gi['终止最低价']) == g['stop_low']
    assert float(gi['终止最高价']) == g['stop_high']


def test_simulate_grid_engine_matches_golden():
    from gridtrade.core.grid_engine import simulate_grid_engine
    g = _golden()['simulate']
    bars = make_1m_bars()
    grid_params = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 40,
                   'stop_low_price': 88.0, 'stop_high_price': 112.0}
    stop_cfg = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
                'fundingRate_stop_loss': 0.0015}
    res = simulate_grid_engine(bars, grid_params, cap=1000.0, leverage=5.0, stop_cfg=stop_cfg)
    assert abs(res['pnl_ratio'] - g['pnl_ratio']) < 1e-9
    assert abs(res['net_value_final'] - g['net_value_final']) < 1e-9
    assert res['exit_reason'] == g['exit_reason']
    assert int(res['n_trades']) == g['n_trades']
    assert bool(res['broke']) == g['broke']
    assert bool(res['terminated']) == g['terminated']
    assert bool(res['blown_up']) == g['blown_up']
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/core/test_grid_engine_parity.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.core.grid_engine`）。

- [ ] **Step 3: 逐字复制引擎**

执行：
```bash
cp backtest/grid_engine.py gridtrade/core/grid_engine.py
```
不要改动 `gridtrade/core/grid_engine.py` 的任何内容（它只 import datetime/numpy/pandas，无交易所依赖）。不要修改 `backtest/grid_engine.py`。

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/core/test_grid_engine_parity.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/core/grid_engine.py tests/core/test_grid_engine_parity.py
git commit -m "feat(core): migrate neutral grid engine to core.grid_engine (golden parity)"
```

---

### Task 3: 标量退出评估器 core/stop_rules.evaluate_exit（与 _apply_exit 等价）

**Files:**
- Create: `gridtrade/core/stop_rules.py`
- Create: `tests/core/test_stop_rules.py`

**Interfaces:**
- Consumes: `gridtrade.core.grid_engine._apply_exit`
- Produces: `gridtrade.core.stop_rules.evaluate_exit`：
  - 签名 `evaluate_exit(pnl_ratio, pnl_ratio_max, *, net_value, stop_cfg=None, margin_rate=0.05, funding_rate=None, pv_spike=0) -> Optional[str]`
  - 返回退出原因字符串（`'固定止损'/'连续回撤止盈'/'资金费率止损'/'pv主动止损'/'爆仓'`）或 `None`。优先级与 `_apply_exit` 同序。

- [ ] **Step 1: 写等价测试**

Create `tests/core/test_stop_rules.py`:

```python
import numpy as np
import pandas as pd


STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
            'fundingRate_stop_loss': 0.0015}
MARGIN = 0.05
CAP = 1000.0
C_RATE = 0.0005


def _make_df(net_values, funding=None, pv=None):
    n = len(net_values)
    t = pd.date_range('2024-03-01', periods=n, freq='1min')
    df = pd.DataFrame({
        'candle_begin_time': t,
        'net_value': np.asarray(net_values, dtype='float64'),
        'hold_num': np.ones(n),       # _apply_exit 平仓扣费用得到，等价比对不依赖其值
        'close': np.full(n, 100.0),
    })
    if funding is not None:
        df['fundingRate'] = np.asarray(funding, dtype='float64')
    pv_df = None
    if pv is not None:
        pv_df = pd.DataFrame({'candle_begin_time': t, 'pv_spike': np.asarray(pv, dtype='int64')})
    return df, pv_df


def _scalar_first(df, pv_df):
    """逐行扫描 evaluate_exit，返回首个触发的 (reason, idx) 或 (None, None)。"""
    from gridtrade.core.stop_rules import evaluate_exit
    pr = (df['net_value'] - 1.0).values
    pr_max = np.maximum.accumulate(pr)
    pv_map = {}
    if pv_df is not None:
        pv_map = dict(zip(pv_df['candle_begin_time'], pv_df['pv_spike']))
    for i in range(len(df)):
        fr = float(df['fundingRate'].iloc[i]) if 'fundingRate' in df.columns else None
        pv = int(pv_map.get(df['candle_begin_time'].iloc[i], 0))
        r = evaluate_exit(float(pr[i]), float(pr_max[i]),
                          net_value=float(df['net_value'].iloc[i]),
                          stop_cfg=STOP_CFG, margin_rate=MARGIN, funding_rate=fr, pv_spike=pv)
        if r is not None:
            return r, i
    return None, None


def _assert_equiv(net_values, funding=None, pv=None):
    from gridtrade.core.grid_engine import _apply_exit
    df, pv_df = _make_df(net_values, funding, pv)
    truncated, reason, blown = _apply_exit(df.copy(), CAP, C_RATE, STOP_CFG, MARGIN, pv_df)
    s_reason, s_idx = _scalar_first(df, pv_df)
    assert s_reason == reason, f'reason mismatch: scalar={s_reason} apply_exit={reason}'
    if reason is None:
        assert s_idx is None
    else:
        assert s_idx == len(truncated) - 1, f'idx mismatch: scalar={s_idx} apply_exit={len(truncated)-1}'


def test_no_trigger_runs_to_end():
    _assert_equiv([1.0, 1.002, 1.001, 1.003, 1.002])


def test_fixed_stop_loss():
    _assert_equiv([1.0, 0.99, 0.97, 0.96, 0.95])  # 跌破 -3.4%


def test_chandelier_trailing():
    # 先冲高再回撤：峰值 +5%，回撤超过 max(0.618%, 30%×5%)=1.5%
    _assert_equiv([1.0, 1.02, 1.05, 1.045, 1.03])


def test_funding_rate_stop():
    _assert_equiv([1.0, 1.001, 1.002, 1.001],
                  funding=[0.0, 0.0, 0.002, 0.0])  # |0.002| > 0.0015


def test_pv_active_stop():
    _assert_equiv([1.0, 0.99, 0.98, 0.985],
                  pv=[0, 0, 1, 0])  # pv_spike 且 pnl<-0.015


def test_liquidation():
    _assert_equiv([1.0, 0.5, 0.04, 0.03])  # net_value < 0.05


def test_priority_fixed_over_chandelier_same_bar():
    # bar2 同时满足固定止损(-4%<-3.4%)与回撤止盈；固定止损优先；前两 bar 不触发
    _assert_equiv([1.0, 1.007, 0.96])
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/core/test_stop_rules.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.core.stop_rules`）。

- [ ] **Step 3: 写 stop_rules.py**

Create `gridtrade/core/stop_rules.py`:

```python
"""标量退出评估器：把 grid_engine._apply_exit 的逐 bar 优先级判定提成一个标量函数，
供实盘监控按当前 (pnl_ratio, pnl_ratio_max, net_value, funding_rate, pv_spike) 判定止盈止损。
优先级与 _apply_exit 完全同序（由 tests/core/test_stop_rules.py 等价测试锁定）。
"""
from typing import Optional


def evaluate_exit(pnl_ratio: float, pnl_ratio_max: float, *, net_value: float,
                  stop_cfg: Optional[dict] = None, margin_rate: float = 0.05,
                  funding_rate: Optional[float] = None, pv_spike: int = 0) -> Optional[str]:
    """返回退出原因或 None。优先级：固定止损 > 连续回撤止盈 > 资金费率止损 > pv主动止损 > 爆仓。
    stop_cfg=None 时仅查爆仓（与 _apply_exit 一致）。"""
    if stop_cfg is not None:
        if pnl_ratio < -stop_cfg['stop_loss']:
            return '固定止损'
        k = stop_cfg.get('trailing_k')
        floor = stop_cfg.get('trailing_floor')
        if k is not None and floor is not None:
            allowed = max(floor, k * pnl_ratio_max)
            if (pnl_ratio_max - pnl_ratio >= allowed) and (pnl_ratio_max > floor):
                return '连续回撤止盈'
        fr_thr = stop_cfg.get('fundingRate_stop_loss')
        if fr_thr is not None and funding_rate is not None:
            if abs(funding_rate) > fr_thr:
                return '资金费率止损'
        if pv_spike == 1 and pnl_ratio < -0.015:
            return 'pv主动止损'
    if net_value < margin_rate:
        return '爆仓'
    return None
```

- [ ] **Step 4: 运行确认通过 + 全套回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/core/test_stop_rules.py -v`
Expected: PASS（7 passed）。

Run（全仓回归）: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全 PASS（既有 55 + 本计划新增 ≈ 11）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/core/stop_rules.py tests/core/test_stop_rules.py
git commit -m "feat(core): add scalar evaluate_exit equivalent to grid_engine._apply_exit"
```

---

## 完成判定（P3a）

- `pytest -q` 全绿：grid_engine 零漂移金标 + evaluate_exit 与 _apply_exit 等价（覆盖固定/回撤/资金费/pv/爆仓/无触发/同 bar 优先级）。
- `gridtrade/core/` 不 import 任何交易所库（`grep -rnE "ccxt|hyperliquid|requests" gridtrade/core` 仅命中注释文字，无真实 import）。
- `core/grid_engine.py` 与 `backtest/grid_engine.py` 逐字一致；`backtest/` 未被修改。

## 后续（不在本计划内）

P3b：`execution/live_equity.py`（增量记账，复用 `core.grid_engine.cal_equity_curve` 计算当前 pnl_ratio，资金费独立累计）+ `execution/grid_executor.py`（挂单网格生命周期状态机，驱动 ExchangeAdapter+StateStore，针对 FakeExchange TDD）+ `execution/reconciler.py`（重启对账自愈）。其中实盘资金费记账口径若有不确定，P3b 开始前先与用户确认。
