# 交易所解耦重构 P4c 实现计划（触发引擎 + ScheduledSelectionTrigger）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现「触发 → 准入 → 执行」三段式的**触发层**（design.md §6①）：可插拔 `TriggerCondition`（Strategy）+ `TriggerEngine`（汇集多触发器提议）+ 内置 `ScheduledSelectionTrigger`（offset + 因子选币，主流程「原样保留」）。触发器只**提议**（产出 `GridProposal`），不下单、不过门、不开仓。`ThresholdTrigger`/`ExternalSignalTrigger` 待产品定义，本增量不做（接口已留）。

**Architecture:** `ScheduledSelectionTrigger` 复刻 legacy `account_0` 主流程的提议切片：`compute_offset → 选币（proceed_calc_symbol_factor + select_grid_coin）→ 每个选中行用 calc_grid_params_v1/v2 算网格几何 → GridProposal`。**关键设计**：触发器产出 **raw-float** grid_params（直接来自已金标的 `core.grid_params`），**不做 tick 精度格式化**——精度由适配器（ccxt markets）在下单时负责（design.md §3），`GridExecutor.open` 也只吃 float 参数。选币重计算（proceed_calc_symbol_factor/select_grid_coin）已被 golden 测试锁定，本增量用注入的 `select_fn` 隔离测试触发器自身新逻辑（offset/版本选择/行→提议映射/tag）。

**Tech Stack:** Python 3.9、pandas 1.3.5、dataclasses、abc、pytest、内存 SQLite（不涉及）。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（提议字段、选币列名、版本选择口径、tag 规则、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；`gridtrade/execution/` 不得 import 交易所库（触发器只吃已拉好的 `symbol_candle_data` DataFrame + 配置）。
- 只新增 `gridtrade/execution/triggers.py` 及 `tests/execution/test_triggers.py`；不改 `core/`、`state/`、`exchanges/`、`backtest/`、已有 `execution/*`（含 `gates.py`，仅 import 其 `GridProposal`）。
- `GridProposal` 复用 `gridtrade.execution.gates.GridProposal`（不重新定义）。
- 触发器产出 **raw-float** grid_params（`calc_grid_params_v1/v2` 原样输出，键 = high_price/low_price/stop_high_price/stop_low_price/grid_count）；**不做** tick 格式化与 legacy 的 round 碰撞护栏（移到适配器下单层）。
- 版本选择口径同 legacy `generate_order_info`：`grid_version == 2` 用 v2 否则 v1；调用一律传 `v2_config=strategy_config.get('grid_v2_config', {})`（v1 经 `**kwargs` 忽略）。
- tag 规则同 legacy：`f'{strategy_tag}{offset}'`；offset = `compute_offset(run_time, period, utc_offset)`。
- point-in-time 新鲜度过滤复刻 `selection_replay.py:63`：`factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= run_time]`。
- 选币为空 / 无候选 → 返回 `[]`（不抛）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。全量回归须保持绿（基线 138 passed）。

---

## 文件结构（本计划新建）

```
gridtrade/execution/
  triggers.py     # 新增：TriggerContext / TriggerCondition(ABC) / TriggerEngine
                  #       + ScheduledSelectionTrigger
tests/execution/
  test_triggers.py
```

`triggers.py` 公共接口（供 P4d GridManager / P4e scheduler 消费）：

```python
@dataclass
class TriggerContext:
    exchange: str
    run_time: pd.Timestamp
    symbol_candle_data: Optional[dict] = None   # {symbol: candle_df}

class TriggerCondition(ABC):
    @abstractmethod
    def propose(self, ctx: TriggerContext) -> List[GridProposal]: ...

class TriggerEngine:
    def __init__(self, triggers: Iterable[TriggerCondition]): ...
    def collect(self, ctx: TriggerContext) -> List[GridProposal]: ...  # 汇集所有触发器提议

class ScheduledSelectionTrigger(TriggerCondition):
    def __init__(self, strategy_config: dict, factors: dict, weight_list: list, *,
                 utc_offset: int = 8, select_fn=None,
                 source: str = 'ScheduledSelectionTrigger'): ...
```

---

### Task 1: TriggerContext + TriggerCondition + TriggerEngine（汇集语义）

**Files:**
- Create: `gridtrade/execution/triggers.py`
- Create: `tests/execution/test_triggers.py`

**Interfaces:**
- Consumes: `gridtrade.execution.gates.GridProposal`；标准库 `dataclasses/abc/typing`；`pandas`。
- Produces: `TriggerContext`、`TriggerCondition`(ABC)、`TriggerEngine`（见上签名）。

- [ ] **Step 1: 写失败测试**

Create `tests/execution/test_triggers.py`:

```python
import pandas as pd

from gridtrade.execution.gates import GridProposal
from gridtrade.execution.triggers import (TriggerContext, TriggerCondition,
                                          TriggerEngine)


def _ctx(**kw):
    base = dict(exchange='okx', run_time=pd.Timestamp('2025-06-24 14:00:00'))
    base.update(kw)
    return TriggerContext(**base)


def _prop(symbol, source):
    return GridProposal(exchange='okx', symbol=symbol,
                        grid_params={'low_price': 1.0, 'high_price': 2.0,
                                     'grid_count': 5, 'stop_low_price': 0.5,
                                     'stop_high_price': 2.5},
                        source=source)


class _FixedTrigger(TriggerCondition):
    def __init__(self, props):
        self._props = props
    def propose(self, ctx):
        return list(self._props)


def test_engine_concatenates_all_trigger_proposals():
    t1 = _FixedTrigger([_prop('BTC/USDT:USDT', 't1')])
    t2 = _FixedTrigger([_prop('ETH/USDT:USDT', 't2'),
                        _prop('SOL/USDT:USDT', 't2')])
    engine = TriggerEngine([t1, t2])
    out = engine.collect(_ctx())
    assert [p.symbol for p in out] == ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
    assert [p.source for p in out] == ['t1', 't2', 't2']


def test_engine_empty_triggers_returns_empty():
    assert TriggerEngine([]).collect(_ctx()) == []


def test_engine_passes_same_context_to_each_trigger():
    seen = []
    class _Spy(TriggerCondition):
        def propose(self, ctx):
            seen.append(ctx)
            return []
    ctx = _ctx()
    TriggerEngine([_Spy(), _Spy()]).collect(ctx)
    assert seen == [ctx, ctx]
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_triggers.py -q`
Expected: FAIL（`ModuleNotFoundError: gridtrade.execution.triggers`）。

- [ ] **Step 3: 实现 triggers.py 骨架**

Create `gridtrade/execution/triggers.py`:

```python
"""触发引擎 —— 「触发 → 准入 → 执行」三段式的触发层（只提议，不下单）。

TriggerCondition 是可插拔策略：吃 TriggerContext，吐 GridProposal 列表。
TriggerEngine 汇集所有已注册触发器的提议，交给准入门链（gates.GateChain）过闸。
ScheduledSelectionTrigger 复刻 legacy 主流程的选币提议切片（主流程原样保留）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, List, Optional

import pandas as pd

from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2
from gridtrade.core.selection import (compute_offset, proceed_calc_symbol_factor,
                                      select_grid_coin)
from gridtrade.execution.gates import GridProposal


@dataclass
class TriggerContext:
    exchange: str
    run_time: pd.Timestamp
    symbol_candle_data: Optional[dict] = None


class TriggerCondition(ABC):
    @abstractmethod
    def propose(self, ctx: TriggerContext) -> List[GridProposal]:
        ...


class TriggerEngine:
    def __init__(self, triggers: Iterable[TriggerCondition]):
        self.triggers: List[TriggerCondition] = list(triggers)

    def collect(self, ctx: TriggerContext) -> List[GridProposal]:
        proposals: List[GridProposal] = []
        for trigger in self.triggers:
            proposals.extend(trigger.propose(ctx))
        return proposals
```

- [ ] **Step 4: 跑测试确认绿**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_triggers.py -q`
Expected: 3 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/triggers.py tests/execution/test_triggers.py
git commit -m "feat(execution): TriggerCondition + TriggerEngine skeleton (P4c)"
```

---

### Task 2: ScheduledSelectionTrigger（offset + 选币 → 提议，金标对齐 legacy）

**Files:**
- Modify: `gridtrade/execution/triggers.py`
- Modify: `tests/execution/test_triggers.py`

**Interfaces:**
- Consumes: `compute_offset(run_time, period, utc_offset)`、`calc_grid_params_v1/v2(row, price_limit, stop_limit, v2_config=...)`、`proceed_calc_symbol_factor`、`select_grid_coin`（默认 select_fn 组合用）。
- Produces: `ScheduledSelectionTrigger(strategy_config, factors, weight_list, *, utc_offset=8, select_fn=None, source='ScheduledSelectionTrigger')`，`propose(ctx) -> List[GridProposal]`。
- `select_fn` 签名（可注入隔离测试）：`select_fn(symbol_candle_data, run_time, offset) -> pandas.DataFrame`（列含 symbol/rank/time/close/Atr_5/middle_5）；默认组合 `proceed_calc_symbol_factor + select_grid_coin`。

- [ ] **Step 1: 写失败测试**

在 `tests/execution/test_triggers.py` 末尾追加：

```python
def _strategy_config(**kw):
    base = dict(period='12H', strategy_tag='acc1at', choose_symbols=3,
                price_limit=[0.1, 0.1], stop_limit=0.05, leverage=3,
                grid_version=1, grid_v2_config={}, weight_list=[1, 1],
                max_candle_num=100)
    base.update(kw)
    return base


def _factor_row(symbol, rank, run_time, close=100.0, atr_5=0.02, middle_5=100.0):
    return {'symbol': symbol, 'rank': rank, 'time': run_time,
            'close': close, 'Atr_5': atr_5, 'middle_5': middle_5}


def test_scheduled_trigger_maps_selection_rows_to_proposals_v1():
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    run_time = pd.Timestamp('2025-06-24 14:00:00')
    rows = pd.DataFrame([_factor_row('BTC/USDT:USDT', 1, run_time),
                         _factor_row('ETH/USDT:USDT', 2, run_time, close=200.0)])
    trig = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True, 'Sgcz_2': True},
                                     [1, 1], utc_offset=8,
                                     select_fn=lambda scd, rt, off: rows)
    out = trig.propose(TriggerContext(exchange='okx', run_time=run_time,
                                      symbol_candle_data={'BTC/USDT:USDT': None}))
    assert [p.symbol for p in out] == ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    # raw-float 网格几何（close=100, atr=0.02, middle=100, price_limit=0.1, stop=0.05）
    btc = out[0].grid_params
    assert btc['high_price'] == 106.0 and btc['low_price'] == 94.0
    assert btc['stop_high_price'] == 115.5 and btc['stop_low_price'] == 85.5
    assert btc['grid_count'] == 9
    # 提议元数据：source / tag / exchange / offset
    assert out[0].source == 'ScheduledSelectionTrigger'
    assert out[0].exchange == 'okx'
    # offset = compute_offset(run_time, '12H', 8); tag = 'acc1at%d' % offset
    from gridtrade.core.selection import compute_offset
    off = compute_offset(run_time, '12H', 8)
    assert out[0].offset == off and out[0].tag == 'acc1at%d' % off


def test_scheduled_trigger_empty_selection_returns_empty():
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    run_time = pd.Timestamp('2025-06-24 14:00:00')
    trig = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True}, [1],
                                     select_fn=lambda scd, rt, off: pd.DataFrame())
    out = trig.propose(TriggerContext(exchange='okx', run_time=run_time))
    assert out == []


def test_scheduled_trigger_v2_uses_v2_params():
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    from gridtrade.core.grid_params import calc_grid_params_v2
    run_time = pd.Timestamp('2025-06-24 14:00:00')
    v2cfg = {'atr_range_multiplier': 2.0, 'range_pct_min': 0.01, 'range_pct_max': 0.2,
             'grid_spacing_atr_ratio': 0.5, 'grid_spacing_min': 0.005,
             'grid_spacing_max': 0.05, 'grid_count_min': 5, 'grid_count_max': 100,
             'stop_buffer_ratio': 0.1}
    cfg = _strategy_config(grid_version=2, grid_v2_config=v2cfg)
    row = _factor_row('BTC/USDT:USDT', 1, run_time)
    rows = pd.DataFrame([row])
    trig = ScheduledSelectionTrigger(cfg, {'Reg_v2_2': True}, [1],
                                     select_fn=lambda scd, rt, off: rows)
    out = trig.propose(TriggerContext(exchange='okx', run_time=run_time))
    expected = calc_grid_params_v2(row=row, price_limit=[0.1, 0.1], stop_limit=0.05,
                                   v2_config=v2cfg)
    assert out[0].grid_params == expected


def test_scheduled_trigger_sorts_by_rank():
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    run_time = pd.Timestamp('2025-06-24 14:00:00')
    # 乱序 rank，断言提议按 rank 升序
    rows = pd.DataFrame([_factor_row('C/USDT:USDT', 3, run_time),
                         _factor_row('A/USDT:USDT', 1, run_time),
                         _factor_row('B/USDT:USDT', 2, run_time)])
    trig = ScheduledSelectionTrigger(_strategy_config(), {'Reg_v2_2': True}, [1],
                                     select_fn=lambda scd, rt, off: rows)
    out = trig.propose(TriggerContext(exchange='okx', run_time=run_time))
    assert [p.symbol for p in out] == ['A/USDT:USDT', 'B/USDT:USDT', 'C/USDT:USDT']
```

- [ ] **Step 2: 跑测试确认红**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_triggers.py -k scheduled -q`
Expected: FAIL（`ImportError: cannot import name 'ScheduledSelectionTrigger'`）。

- [ ] **Step 3: 实现 ScheduledSelectionTrigger**

在 `gridtrade/execution/triggers.py` 末尾追加：

```python
def _default_select_fn(strategy_config, factors, weight_list):
    period = strategy_config['period']
    choose_symbols = strategy_config['choose_symbols']

    def _fn(symbol_candle_data, run_time, offset):
        all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time,
                                            period, offset)
        if all_df is None or all_df.empty:
            return all_df
        return select_grid_coin(all_df, factors, weight_list, choose_symbols,
                                run_time)

    return _fn


class ScheduledSelectionTrigger(TriggerCondition):
    """offset + 因子选币 → 网格提议（legacy 主流程原样保留）。

    产出 raw-float grid_params（来自已金标的 core.grid_params），tick 精度由适配器
    下单层负责，本触发器不格式化、不套用 legacy 的 round 碰撞护栏。
    """

    def __init__(self, strategy_config, factors, weight_list, *,
                 utc_offset=8, select_fn=None,
                 source='ScheduledSelectionTrigger'):
        self.strategy_config = strategy_config
        self.factors = factors
        self.weight_list = weight_list
        self.utc_offset = int(utc_offset)
        self.source = source
        self.select_fn = select_fn or _default_select_fn(
            strategy_config, factors, weight_list)

    def propose(self, ctx: TriggerContext) -> List[GridProposal]:
        cfg = self.strategy_config
        period = cfg['period']
        offset = compute_offset(ctx.run_time, period, self.utc_offset)
        factor_data = self.select_fn(ctx.symbol_candle_data, ctx.run_time, offset)
        if factor_data is None or factor_data.empty:
            return []
        # point-in-time 新鲜度过滤（同 selection_replay）
        factor_data = factor_data[
            (factor_data['time'] + pd.to_timedelta(period)) >= ctx.run_time]
        if factor_data.empty:
            return []
        factor_data = factor_data.sort_values('rank')

        grid_version = cfg.get('grid_version', 1)
        calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1
        price_limit = cfg['price_limit']
        stop_limit = cfg['stop_limit']
        v2_config = cfg.get('grid_v2_config', {})
        tag = '%s%d' % (cfg['strategy_tag'], offset)

        proposals: List[GridProposal] = []
        for _, row in factor_data.iterrows():
            params = calc_fn(row=row, price_limit=price_limit,
                             stop_limit=stop_limit, v2_config=v2_config)
            proposals.append(GridProposal(
                exchange=ctx.exchange, symbol=row['symbol'], grid_params=params,
                offset=offset, tag=tag, source=self.source))
        return proposals
```

- [ ] **Step 4: 跑测试确认绿 + 全量回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/execution/test_triggers.py -q`
Expected: 全 PASS（7：3 引擎 + 4 触发器）。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全量 PASS（≥ 138 + 新增触发器测试）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/execution/triggers.py tests/execution/test_triggers.py
git commit -m "feat(execution): ScheduledSelectionTrigger offset+selection -> proposals (P4c)"
```

---

## Self-Review

- **Spec 覆盖**：design.md §6① 触发引擎（Strategy + 可插拔 TriggerCondition）—— `TriggerCondition` ABC + `TriggerEngine`（Task 1）+ `ScheduledSelectionTrigger`（Task 2，主流程原样保留）。`ThresholdTrigger/ExternalSignalTrigger` 显式延后（需产品定义），`TriggerCondition` 接口已留。
- **Golden 对齐**：版本选择（v1/v2）、price_limit/stop_limit 入参、tag=`{strategy_tag}{offset}`、offset=`compute_offset`、新鲜度过滤均复刻 legacy `generate_order_info` / `selection_replay`。grid_params 直用已金标的 `calc_grid_params_v1/v2`。
- **设计偏离记录**：触发器产出 raw-float（不做 legacy 的 tick 格式化 + round 碰撞护栏）——精度移到适配器下单层（design.md §3）；`GridExecutor.open` 吃 float 同此约定。
- **Placeholder 扫描**：无 TBD/TODO；每步完整代码 + 精确命令/预期。
- **类型一致**：`GridProposal` 复用 gates 定义；`TriggerContext` 字段（exchange/run_time/symbol_candle_data）与 propose 读取一致；`select_fn(symbol_candle_data, run_time, offset)` 签名在默认实现与测试注入间一致。
