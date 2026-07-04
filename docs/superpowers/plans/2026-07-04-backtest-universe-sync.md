# 回测票池与 prod 同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让回测候选票池按 prod 同口径动态生成——全市场 −黑名单 −逐 run_time PIT `$1M` 成交额地板（从缓存 1h K 线的前置 24h `quote_volume` 重建）；选币数学不动、仅回测。

**Architecture:** ②在 `selection_replay` 逐 run_time 加 PIT 绝对成交额地板+黑名单（抽 `build_pit_candidates` 纯函数）；①候选由 `list_instruments`（swap+去重）−黑名单 全市场化；③把 `build_grid_tasks` 拆成 `select_grids`+`assemble_grid_tasks`，`main` 编排两段式预热（1h 全市场→选币→仅选中币 1m/funding）。

**Tech Stack:** Python 3.9、pandas、pytest、ParquetCache、HL ccxt（仅 prewarm/main 惰性导入）。

## Global Constraints

- 运行测试：`TZ=Asia/Shanghai /Users/thomaschang/Projects/GridTradeGP/.venv/bin/python -m pytest`。
- 依赖冻结（py3.9 / pandas 1.3.5）。
- **不改** `core.selection`（因子/55%/排名/offset）、`grid_params`、止损、prod/live 路径。仅动 `gridtrade/backtest/`。
- 新参 `min_quote_volume=0.0` / `blacklist=()` **默认关**——保持现有回测行为与测试向后兼容（旧测试不改）。
- PIT 纪律：地板只用 `candle_begin_time < run_time` 的 bar；`tail(24)` 求 24h `quote_volume` 和。
- 地板阈值 `BT_MIN_QUOTE_VOLUME_24H` 默认 **1_000_000.0**（对齐 prod）；`quote_volume`(≈量×midprice)之和是 live `dayNtlVlm` 的近似。
- 分支 `backtest-universe-sync`；不 push `production`。

---

### Task 1: ② `selection_replay` PIT 地板 + 黑名单（抽 `build_pit_candidates`）

**Files:**
- Modify: `gridtrade/backtest/selection_replay.py:30-56`
- Test: `tests/backtest/test_selection_replay.py`

**Interfaces:**
- Produces: `build_pit_candidates(series, run_time, *, max_candle_num, min_quote_volume=0.0, blacklist=()) -> dict[str, DataFrame]`；`replay_selection(cache, symbols, run_times, strategy_config, factors, on_select, *, timeframe='1h', min_quote_volume=0.0, blacklist=(), log=print)`。

- [ ] **Step 1: 写 `build_pit_candidates` 的失败测试**

`tests/backtest/test_selection_replay.py` 末尾追加：

```python
def _series_with_vol(symbol, n=60, vol_per_bar=1.0, start='2024-01-01'):
    import pandas as pd, numpy as np
    t = pd.date_range(start, periods=n, freq='1H')
    close = np.full(n, 100.0)
    return pd.DataFrame({
        'symbol': symbol, 'candle_begin_time': t,
        'open': close, 'high': close * 1.001, 'low': close * 0.999, 'close': close,
        'vol': 1.0, 'volCcy': 1.0, 'quote_volume': float(vol_per_bar),
    })


def test_build_pit_candidates_floor_and_blacklist_and_pit():
    import pandas as pd
    from gridtrade.backtest.selection_replay import build_pit_candidates
    # HIGH: 每根 100 → 前置24根和=2400；LOW: 每根 10 → 24根和=240
    series = {'HIGH/USDC:USDC': _series_with_vol('HIGH/USDC:USDC', vol_per_bar=100.0),
              'LOW/USDC:USDC':  _series_with_vol('LOW/USDC:USDC',  vol_per_bar=10.0),
              'BAN/USDC:USDC':  _series_with_vol('BAN/USDC:USDC',  vol_per_bar=100.0)}
    rt = pd.Timestamp('2024-01-03 00:00:00')   # 有 >24 根 < rt
    # 门槛 1000：HIGH(2400)过、LOW(240)剔；BAN 被黑名单剔
    out = build_pit_candidates(series, rt, max_candle_num=160,
                               min_quote_volume=1000.0, blacklist=('BAN/USDC:USDC',))
    assert set(out) == {'HIGH/USDC:USDC'}
    # 门槛 0=停用：HIGH+LOW 都在（BAN 仍被黑名单剔）
    out0 = build_pit_candidates(series, rt, max_candle_num=160,
                                min_quote_volume=0.0, blacklist=('BAN/USDC:USDC',))
    assert set(out0) == {'HIGH/USDC:USDC', 'LOW/USDC:USDC'}


def test_build_pit_candidates_no_lookahead():
    import pandas as pd
    from gridtrade.backtest.selection_replay import build_pit_candidates
    # 前 30 根量=10（和=240<1000），第 30 根后量飙到 1000。run_time 卡在飙升前 → 仍按低量剔。
    import numpy as np
    t = pd.date_range('2024-01-01', periods=60, freq='1H')
    qv = np.concatenate([np.full(30, 10.0), np.full(30, 1000.0)])
    df = pd.DataFrame({'symbol': 'X/USDC:USDC', 'candle_begin_time': t,
                       'open': 100.0, 'high': 100.1, 'low': 99.9, 'close': 100.0,
                       'vol': 1.0, 'volCcy': 1.0, 'quote_volume': qv})
    rt = t[28]   # 只看得到前 28 根（都是 10）
    out = build_pit_candidates({'X/USDC:USDC': df}, rt, max_candle_num=160, min_quote_volume=1000.0)
    assert out == {}          # 未来的高量不算进来（无未来函数）
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_selection_replay.py::test_build_pit_candidates_floor_and_blacklist_and_pit -q`
Expected: FAIL（`build_pit_candidates` 未定义 → ImportError）

- [ ] **Step 3: 实现 `build_pit_candidates` + 让 `replay_selection` 用它、加参数**

`gridtrade/backtest/selection_replay.py`：在 `load_full_series` 之后、`replay_selection` 之前插入：

```python
def build_pit_candidates(series, run_time, *, max_candle_num,
                         min_quote_volume=0.0, blacklist=()):
    """逐 run_time 构造候选 K 线字典：PIT 截断(<run_time) + ≥24 根 + 绝对成交额地板 + 黑名单。
    绝对地板 = 前置 24 根 1h bar 的 quote_volume 之和（live dayNtlVlm 的缓存重建近似）。"""
    bl = set(blacklist)
    out = {}
    for s, df in series.items():
        if s in bl:                                   # 档0：无条件硬禁
            continue
        sub = df[df['candle_begin_time'] < run_time]  # PIT，无未来函数
        if len(sub) < 24:
            continue
        if min_quote_volume and min_quote_volume > 0:  # PIT 绝对成交额地板
            if float(sub.tail(24)['quote_volume'].sum()) < min_quote_volume:
                continue
        out[s] = sub.tail(max_candle_num).copy()
    return out
```

`replay_selection` 签名（30-31 行）改为：

```python
def replay_selection(cache, symbols, run_times, strategy_config, factors, on_select, *,
                     timeframe='1h', min_quote_volume=0.0, blacklist=(), log=print):
```

把 47-53 行的内联候选构造（`symbol_candle_data = {}` 到 `symbol_candle_data[s] = sub.tail(...)`）替换为：

```python
            symbol_candle_data = build_pit_candidates(
                series, run_time, max_candle_num=max_candle_num,
                min_quote_volume=min_quote_volume, blacklist=blacklist)
```

- [ ] **Step 4: 运行确认通过（含现有 replay 测试）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_selection_replay.py -q`
Expected: PASS（新 2 测试 + 现有 `test_load_full_series`/`test_replay_selection_emits_picks` 均绿——后者未传新参、默认 0/空、行为不变）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/selection_replay.py tests/backtest/test_selection_replay.py
git commit -m "feat(backtest): selection_replay PIT 成交额地板+黑名单（build_pit_candidates，②）"
```

---

### Task 2: 把地板参数穿过 `build_grid_tasks` + `run_backtest`

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py:95-113,156-167`
- Test: `tests/backtest/test_backtest_run.py`

**Interfaces:**
- Consumes: `replay_selection(..., min_quote_volume, blacklist)`（Task 1）。
- Produces: `build_grid_tasks(cache, universe, window_start, window_end, strategy_config, factors, *, timeframe='1h', sim_timeframe=None, min_quote_volume=0.0, blacklist=(), log=print)`；`run_backtest(..., min_quote_volume=0.0, blacklist=())`。

- [ ] **Step 1: 写地板过滤的 e2e 失败测试**

`tests/backtest/test_backtest_run.py` 末尾追加：

```python
def test_run_backtest_min_quote_volume_filters(tmp_path):
    import numpy as np
    from gridtrade.backtest.cache import ParquetCache
    from gridtrade.backtest.backtest_run import run_backtest
    from gridtrade.exchanges.base import CANDLE_COLS
    # 两币：RICH quote_volume 大、POOR 极小。造 300 根 1h。
    def _bars(sym, qv):
        t = pd.date_range('2024-01-01', periods=300, freq='1H')
        close = 100.0 * np.exp(np.cumsum(np.random.RandomState(1).normal(0, 0.01, 300)))
        open_ = np.concatenate([[100.0], close[:-1]])
        return pd.DataFrame({'symbol': sym, 'candle_begin_time': t,
                             'open': open_, 'high': np.maximum(open_, close) * 1.001,
                             'low': np.minimum(open_, close) * 0.999, 'close': close,
                             'vol': 1.0, 'volCcy': 1.0, 'quote_volume': float(qv)})[CANDLE_COLS]
    cache = ParquetCache(str(tmp_path))
    for sym, qv in [('RICH/USDC:USDC', 1e5), ('POOR/USDC:USDC', 1.0)]:
        df = _bars(sym, qv)
        for day, g in df.groupby(df['candle_begin_time'].dt.strftime('%Y-%m-%d')):
            cache.write('1h', sym, day, g.reset_index(drop=True))
    syms = ['RICH/USDC:USDC', 'POOR/USDC:USDC']
    # 门槛=1e6：RICH 24h 和 = 24*1e5=2.4e6 过；POOR=24*1=24 剔
    df = run_backtest(cache, syms, pd.Timestamp('2024-01-10 00:00:00'),
                      pd.Timestamp('2024-01-11 00:00:00'), _strategy(), FACTORS,
                      timeframe='1h', min_quote_volume=1_000_000.0)
    assert (df['symbol'] == 'POOR/USDC:USDC').sum() == 0     # POOR 被地板剔、从不入选
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_run.py::test_run_backtest_min_quote_volume_filters -q`
Expected: FAIL（`run_backtest` 还不接受 `min_quote_volume` → TypeError）

- [ ] **Step 3: 穿参**

`gridtrade/backtest/backtest_run.py` `build_grid_tasks` 签名（95-96 行）改为：

```python
def build_grid_tasks(cache, universe, window_start, window_end, strategy_config, factors,
                     *, timeframe='1h', sim_timeframe=None, min_quote_volume=0.0,
                     blacklist=(), log=print):
```

其 `SR.replay_selection(...)` 调用（111-113 行）改为：

```python
    SR.replay_selection(cache, universe, run_times, strategy_config, factors,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, min_quote_volume=min_quote_volume,
                        blacklist=blacklist, log=log)
```

`run_backtest` 签名（156-158 行）加 `min_quote_volume=0.0, blacklist=()`；其 `build_grid_tasks(...)` 调用（165-167 行）加 `min_quote_volume=min_quote_volume, blacklist=blacklist`：

```python
def run_backtest(cache, universe, window_start, window_end, strategy_config, factors,
                 *, timeframe='1h', sim_timeframe=None, fee_rate=0.0005,
                 max_rate=0.5, leverage=None, min_quote_volume=0.0, blacklist=(),
                 workers=1, log=print):
    ...
    tasks = build_grid_tasks(cache, universe, window_start, window_end, strategy_config,
                             factors, timeframe=timeframe, sim_timeframe=sim_timeframe,
                             min_quote_volume=min_quote_volume, blacklist=blacklist, log=log)
```

- [ ] **Step 4: 运行确认通过（含现有 e2e）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_run.py tests/backtest/ -q`
Expected: PASS（新测试 + `test_run_backtest_end_to_end`（未传门槛、默认 0、行为不变）+ 其它回测测试均绿）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/backtest_run.py tests/backtest/test_backtest_run.py
git commit -m "feat(backtest): build_grid_tasks/run_backtest 穿地板参数"
```

---

### Task 3: 拆 `build_grid_tasks` → `select_grids` + `assemble_grid_tasks`（为两段式预热）

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py:95-137`
- Test: `tests/backtest/test_backtest_run.py`

**Interfaces:**
- Produces: `select_grids(cache, universe, window_start, window_end, strategy_config, factors, *, timeframe='1h', min_quote_volume=0.0, blacklist=(), log=print) -> list[(rt, offset, row)]`；`assemble_grid_tasks(cache, grids, strategy_config, *, sim_timeframe=None, timeframe='1h', log=print) -> list[data_task]`；`build_grid_tasks(...)` 保持原签名、内部 = `assemble_grid_tasks(cache, select_grids(...), ...)`。

- [ ] **Step 1: 写拆分等价性测试**

`tests/backtest/test_backtest_run.py` 末尾追加：

```python
def test_select_grids_then_assemble_equals_build_grid_tasks(tmp_path):
    # _seed_cache 已在本文件顶部 import（from tests.backtest.test_selection_replay import _seed_cache, STRAT, FACTORS）
    from gridtrade.backtest.backtest_run import (build_grid_tasks, select_grids,
                                                 assemble_grid_tasks)
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10 00:00:00'), pd.Timestamp('2024-01-11 00:00:00')
    strat = _strategy()
    a = build_grid_tasks(cache, syms, ws, we, strat, FACTORS, timeframe='1h')
    grids = select_grids(cache, syms, ws, we, strat, FACTORS, timeframe='1h')
    b = assemble_grid_tasks(cache, grids, strat, timeframe='1h')
    # 选中集 == build 的组装集（按 (rt,sym) 比对）
    key = lambda tasks: sorted((str(t[0]), t[2]) for t in tasks)
    assert key(a) == key(b)
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_run.py::test_select_grids_then_assemble_equals_build_grid_tasks -q`
Expected: FAIL（`select_grids`/`assemble_grid_tasks` 未定义）

- [ ] **Step 3: 拆分实现**

`gridtrade/backtest/backtest_run.py`：把现有 `build_grid_tasks`（95-137 行）整体替换为下面三个函数：

```python
def select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                 *, timeframe='1h', min_quote_volume=0.0, blacklist=(), log=print):
    """只跑选币回放（1h + PIT 地板 + 黑名单），返回 [(rt, offset, row)]。offline。"""
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, min_quote_volume=min_quote_volume,
                        blacklist=blacklist, log=log)
    log('[BT] picks=%d' % len(grids))
    return grids


def assemble_grid_tasks(cache, grids, strategy_config, *, sim_timeframe=None,
                        timeframe='1h', log=print):
    """由选中 grids 组装每格 data_task（载选中币 sim 序列 + holding_bars + funding 切片）。offline。"""
    sim_tf = sim_timeframe or timeframe
    period = strategy_config['period']
    price_limit = strategy_config['price_limit']
    stop_limit = strategy_config['stop_limit']
    grid_version = strategy_config.get('grid_version', 1)
    v2cfg = strategy_config.get('grid_v2_config', {})
    calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1

    selected = sorted({row['symbol'] for _, _, row in grids})
    series = SR.load_full_series(cache, selected, sim_tf)   # 仅选中币
    funding_by_sym = {}
    data_tasks = []
    for rt, offset, row in grids:
        sym = row['symbol']
        if sym not in series:
            continue
        bars_df = holding_bars(series[sym], rt, period)
        if len(bars_df) == 0:
            continue
        px = calc_fn(row=row, price_limit=price_limit, stop_limit=stop_limit, v2_config=v2cfg)
        gp = dict(low_price=px['low_price'], high_price=px['high_price'],
                  grid_count=px['grid_count'], stop_high_price=px['stop_high_price'],
                  stop_low_price=px['stop_low_price'])
        if sym not in funding_by_sym:
            funding_by_sym[sym] = cache.read_all_days('funding', sym)
        fd = funding_by_sym[sym]
        if fd is not None and not fd.empty:
            lo = int(bars_df['candle_begin_time'].min().value // 1_000_000)
            hi = int(bars_df['candle_begin_time'].max().value // 1_000_000)
            fd = fd[(fd['ts'] >= lo) & (fd['ts'] <= hi)]
        data_tasks.append((rt, int(offset), sym, float(row['close']), gp, bars_df, fd))
    return data_tasks


def build_grid_tasks(cache, universe, window_start, window_end, strategy_config, factors,
                     *, timeframe='1h', sim_timeframe=None, min_quote_volume=0.0,
                     blacklist=(), log=print):
    """选币 + 组装（offline 便捷组合，run_backtest/测试用）。两段式预热见 main()。"""
    grids = select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                         timeframe=timeframe, min_quote_volume=min_quote_volume,
                         blacklist=blacklist, log=log)
    return assemble_grid_tasks(cache, grids, strategy_config,
                               sim_timeframe=sim_timeframe, timeframe=timeframe, log=log)
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/ -q`
Expected: PASS（拆分等价性 + 现有 e2e/floor 测试均绿——`build_grid_tasks` 行为不变）

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/backtest_run.py tests/backtest/test_backtest_run.py
git commit -m "refactor(backtest): 拆 build_grid_tasks 为 select_grids + assemble_grid_tasks（为两段式预热，③）"
```

---

### Task 4: ① 全市场票池解析（−黑名单）+ 回测 config

**Files:**
- Modify: `gridtrade/backtest/prewarm.py:4-10`
- Modify: `gridtrade/backtest/backtest_run.py:22-30`
- Test: `tests/backtest/test_prewarm_universe.py`（新建）

**Interfaces:**
- Produces: `resolve_universe(datasource, *, blacklist=(), quote='USDT', min_list_age_days=15, limit=None)`（加 `blacklist`，先减黑名单）；`backtest_run.BT_MIN_QUOTE_VOLUME_24H=1_000_000.0`、`BT_BLACKLIST=()`（模块常量，可经 env 覆写）。

- [ ] **Step 1: 写 resolve_universe 黑名单测试**

新建 `tests/backtest/test_prewarm_universe.py`：

```python
from gridtrade.exchanges.base import Instrument


class _DS:
    def __init__(self, insts):
        self._insts = insts
    def list_instruments(self):
        return self._insts


def test_resolve_universe_subtracts_blacklist():
    from gridtrade.backtest.prewarm import resolve_universe
    ds = _DS([Instrument('BTC/USDC:USDC', 0.1, 0.001, 0.001, 'live', 0),
              Instrument('ETH/USDC:USDC', 0.1, 0.001, 0.001, 'live', 0),
              Instrument('OLD/USDC:USDC', 0.1, 0.001, 0.001, 'expired', 0)])
    out = resolve_universe(ds, blacklist=('ETH/USDC:USDC',))
    assert out == ['BTC/USDC:USDC']           # live −黑名单，去重排序；OLD 非 live 剔
    assert resolve_universe(ds) == ['BTC/USDC:USDC', 'ETH/USDC:USDC']   # 无黑名单
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_prewarm_universe.py -q`
Expected: FAIL（`resolve_universe` 还不接受 `blacklist` → TypeError）

- [ ] **Step 3: 实现**

`gridtrade/backtest/prewarm.py` 的 `resolve_universe`（4-10 行）替换为：

```python
def resolve_universe(datasource, *, blacklist=(), quote='USDT', min_list_age_days=15, limit=None):
    """返回可交易票池（规范符号）：state=='live' −黑名单，去重排序 + 可选 limit。
    quote / min_list_age_days 为**预留参数、暂未生效**（list_ts 多为 0/未知）。"""
    bl = set(blacklist)
    out = [inst.symbol for inst in datasource.list_instruments()
           if inst.state == 'live' and inst.symbol not in bl]
    out = sorted(set(out))
    return out[:limit] if limit else out
```

`gridtrade/backtest/backtest_run.py`：把写死的 `HL_UNIVERSE`（23-24 行）删除，替换为回测票池 config 常量（放在 `HL_FACTORS` 附近）：

```python
import os as _os
# 回测票池口径对齐 prod：全市场动态 −黑名单 −逐 run_time PIT $1M 成交额地板。
# 票池在 main() 里由 list_instruments 解析（见 main）；此处只放阈值/黑名单常量（可 env 覆写）。
BT_MIN_QUOTE_VOLUME_24H = float(_os.environ.get('BT_MIN_QUOTE_VOLUME_24H', '1000000'))
BT_BLACKLIST = tuple(s.strip() for s in _os.environ.get('BT_BLACKLIST', '').split(',') if s.strip())
```

（`HL_STRATEGY`/`HL_FACTORS` 保留不动。删 `HL_UNIVERSE` 后 main() 里对它的引用在 Task 5 改。）

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_prewarm_universe.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/prewarm.py gridtrade/backtest/backtest_run.py tests/backtest/test_prewarm_universe.py
git commit -m "feat(backtest): resolve_universe 减黑名单 + BT 票池 config 常量（①）"
```

---

### Task 5: `main()` 两段式预热编排（全市场→选币→仅选中币 1m/funding）

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py:174-224,250-290`

**Interfaces:**
- Consumes: `select_grids`/`assemble_grid_tasks`（Task 3）、`resolve_universe`（Task 4）、`BT_MIN_QUOTE_VOLUME_24H`/`BT_BLACKLIST`（Task 4）。

**说明**：`main()` 与 `prewarm_all` 是网络 CLI 路径、**不进单测**（测试走 offline `run_backtest`）。本任务是接线，验证靠"import 不报错 + 结构自洽"，真实端到端靠一次手动网络跑（见 Step 4）。

- [ ] **Step 1: 改 `prewarm_all` 支持分相 + 加 1h/1m 分离入口**

`gridtrade/backtest/backtest_run.py` 的 `prewarm_all`（174-224 行）拆成两个网络函数（保留惰性导入 adapter 构造，抽成小helper避免重复）：

```python
def _hl_datasource_1h(cache):
    """构造带退避的 HL 适配器 + 1h DataSource（网络；惰性导入）。返回 (adapter, ds_1h)。"""
    import time
    import ccxt
    from gridtrade.backtest.datasource import DataSource
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter

    class _RetryHL(HyperliquidAdapter):
        def _retry(self, fn, *a, **k):
            last = None
            for i in range(12):
                try:
                    return fn(*a, **k)
                except (ccxt.ExchangeNotAvailable, ccxt.NetworkError, ccxt.RequestTimeout) as e:
                    last = e
                    time.sleep(min(2.0 * (i + 1), 8.0))
            raise last

        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            return self._retry(super().fetch_ohlcv, symbol, timeframe, start_ms, end_ms)

        def fetch_funding_history(self, symbol, start_ms, end_ms):
            return self._retry(super().fetch_funding_history, symbol, start_ms, end_ms)

    adapter = _RetryHL(ccxt.hyperliquid({'enableRateLimit': True, 'timeout': 30000}))
    return adapter, DataSource(adapter, cache, timeframe='1h')


def prewarm_1h(cache, universe, warm_start_ms, end_ms, *, log=print):
    """phase1：全市场 1h 选币 OHLCV（含暖机）。返回 adapter（复用于 phase2）。"""
    from gridtrade.backtest import prewarm as PW
    adapter, ds_1h = _hl_datasource_1h(cache)
    log('[prewarm] 1h 选币(全市场 %d): %s'
        % (len(universe), PW.prewarm_ohlcv(ds_1h, universe, warm_start_ms, end_ms)))
    return adapter


def prewarm_sim_and_funding(cache, adapter, selected, win_start_ms, end_ms, *,
                            sim_timeframe='1m', log=print):
    """phase2：仅选中币的持仓 OHLCV(1m 走 Reservoir / 其它走 HL) + funding。"""
    from gridtrade.backtest import prewarm as PW
    from gridtrade.backtest.datasource import DataSource
    from gridtrade.backtest.reservoir import warm_reservoir_1m
    sim_tf = sim_timeframe or '1h'
    if sim_tf == '1m':
        sr = warm_reservoir_1m(cache, selected, win_start_ms, end_ms, log=log)
        log('[prewarm] 1m@Reservoir(选中 %d): %s' % (len(selected), sr))
        if sr['rows'] == 0 and sr['skipped_cached'] == 0:
            raise RuntimeError('Reservoir 未拉到任何 1m 数据——检查 AWS 凭证/桶权限/币种 '
                               '(retry_later=%d)' % sr['retry_later'])
    elif sim_tf != '1h':
        ds = DataSource(adapter, cache, timeframe=sim_tf)
        log('[prewarm] %s 持仓(选中 %d): %s'
            % (sim_tf, len(selected), PW.prewarm_ohlcv(ds, selected, win_start_ms, end_ms)))
    ds_1h = DataSource(adapter, cache, timeframe='1h')
    log('[prewarm] funding(选中 %d): %s'
        % (len(selected), PW.prewarm_funding(ds_1h, selected, win_start_ms, end_ms)))
```

保留旧 `prewarm_all`（供既有引用/兼容）——若无其它引用可删；先 grep：`rg -n 'prewarm_all' gridtrade/ tests/`，无外部引用则删除该函数。

- [ ] **Step 2: 改 `main()` 走两段式**

`gridtrade/backtest/backtest_run.py` 的 `main()`（269-278 行的预热+回测段）替换为：

```python
    from gridtrade.backtest.prewarm import resolve_universe
    from gridtrade.backtest.datasource import DataSource

    t0 = time.time()
    # phase1: 解析全市场票池(−黑名单) + 预热全市场 1h
    _adapter, _ds1h = _hl_datasource_1h(cache)
    universe = resolve_universe(_ds1h, blacklist=BT_BLACKLIST)
    print('[BT] 全市场票池 %d 币(−黑名单 %d)' % (len(universe), len(BT_BLACKLIST)))
    from gridtrade.backtest import prewarm as PW
    print('[BT] 1h 预热: %s' % PW.prewarm_ohlcv(_ds1h, universe, _ms(warm_start), _ms(win_end)))

    # 选币(1h + PIT $1M 地板 + 黑名单)——一次
    grids = select_grids(cache, universe, win_start, win_end, HL_STRATEGY, HL_FACTORS,
                         timeframe='1h', min_quote_volume=BT_MIN_QUOTE_VOLUME_24H,
                         blacklist=BT_BLACKLIST)
    selected = sorted({row['symbol'] for _, _, row in grids})
    print('[BT] 选中 %d 币' % len(selected))

    # phase2: 仅选中币预热 1m/funding
    prewarm_sim_and_funding(cache, _adapter, selected, _ms(win_start), _ms(win_end),
                            sim_timeframe=sim_tf)
    print('[BT] prewarm done %.1fs' % (time.time() - t0))

    workers = int(os.environ.get('BT_WORKERS', '1'))
    t0 = time.time()
    tasks = assemble_grid_tasks(cache, grids, HL_STRATEGY,
                                sim_timeframe=(None if sim_tf == '1h' else sim_tf), timeframe='1h')
    df = simulate_tasks(tasks, leverage=HL_STRATEGY['leverage'],
                        stop_cfg=HL_STRATEGY['stop_loss_config'],
                        active_stop_mode=HL_STRATEGY.get('active_stop_mode', 'pv'),
                        pv_cfg=HL_STRATEGY.get('pv_config', {}), workers=workers)
    print('[BT] backtest %.1fs (workers=%d)' % (time.time() - t0, workers))
```

（其余 main() 的窗口解析/summarize/CSV 段不变。）

- [ ] **Step 3: import 冒烟 + 全套回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -c "import gridtrade.backtest.backtest_run as b; print('ok', hasattr(b,'select_grids'), hasattr(b,'prewarm_1h'))"`
Expected: `ok True True`（无 import 错、无残留 `HL_UNIVERSE` 引用——若报 NameError 说明 main 里还有旧引用需清）
Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/ -q`
Expected: PASS（offline 测试不受 main 改动影响）

- [ ] **Step 4: 提交（真实网络端到端留作手动验证）**

```bash
git add gridtrade/backtest/backtest_run.py
git commit -m "feat(backtest): main 两段式预热编排（全市场1h→选币→选中币1m/funding，③）"
```
手动验证（需 HL 网络 + 可选 AWS/Reservoir，**不在 CI**）：
`TZ=Asia/Shanghai .venv/bin/python -m gridtrade.backtest.backtest_run 2026-03-01 2026-03-07 1h`
观察：全市场票池数(~100+)、1h 预热、选中币数、summarize 出网格。

---

### Task 6: 文档/记忆同步

**Files:**
- Modify: `docs/回测使用文档.md`、`docs/STATUS.md`
- Modify: spec 状态；记忆由控制器写

- [ ] **Step 1: 更新回测使用文档**

`docs/回测使用文档.md`：把票池说明从"写死 HL_UNIVERSE 8 币"改为"全市场动态 −黑名单 −逐 run_time PIT `$1M` 成交额地板（`BT_MIN_QUOTE_VOLUME_24H`/`BT_BLACKLIST` env 可覆写），与 prod `resolve_live_universe` 同口径"；示例命令不变（`run_backtest` 现多 `min_quote_volume`/`blacklist` 可选参、默认关）。

- [ ] **Step 2: STATUS.md 加条目**

`docs/STATUS.md` §8 追加：

```markdown
- **回测票池与 prod 同步**：回测候选池从写死 8 币 → 全市场动态（`list_instruments` swap+去重 −黑名单 −逐 run_time PIT `$1M` 成交额地板，地板从缓存 1h `quote_volume` 前置 24h 重建、无未来函数）；`selection_replay.build_pit_candidates` 承载；两段式预热（1h 全市场→选币→仅选中币 1m/funding）。选币数学不动。`BT_MIN_QUOTE_VOLUME_24H`/`BT_BLACKLIST` env 可调。忠实度：candle-vol≈dayNtlVlm 近似 + 存活者偏差。
```

- [ ] **Step 3: spec 状态 + 提交**

`docs/superpowers/specs/2026-07-04-backtest-universe-sync-design.md` 顶部状态 → `已实现`。

```bash
git add docs/回测使用文档.md docs/STATUS.md docs/superpowers/specs/2026-07-04-backtest-universe-sync-design.md
git commit -m "docs(backtest): 回测票池同步文档/STATUS/spec"
```

---

## 自查（对照 spec）

- **Spec 覆盖**：②PIT地板=Task1；穿参=Task2；③拆分=Task3；①全市场+config=Task4；两段式 main=Task5；文档=Task6。✅
- **占位符**：无 TBD；每改码步含完整代码。Task3 Step1 测试有一处示范 import 需按注释删（已标注）。
- **类型一致**：`build_pit_candidates(series, run_time, *, max_candle_num, min_quote_volume=0.0, blacklist=())`、`replay_selection(..., min_quote_volume=0.0, blacklist=())`、`select_grids(...)->grids`、`assemble_grid_tasks(cache, grids, strategy_config, *, sim_timeframe, timeframe)`、`build_grid_tasks` 原签名+2参、`run_backtest`+2参、`resolve_universe(..., blacklist=())`、`BT_MIN_QUOTE_VOLUME_24H`/`BT_BLACKLIST`——定义与调用一致。✅
```
