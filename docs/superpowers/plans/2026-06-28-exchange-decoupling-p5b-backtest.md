# 交易所解耦重构 P5b 实现计划（选币回放 + 回测驱动 + Hyperliquid 验证）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 P5a 数据底座上跑通"按配置交易所"的端到端回测，并用 Hyperliquid 真实验证：① `gridtrade/backtest/selection_replay.py`（point-in-time 选币回放，复用 `core.selection`）；② `gridtrade/backtest/backtest_run.py`（选币→布网→持仓 bars→`simulate_grid_engine`→聚合）；③ `gridtrade/backtest/prewarm.py`（按配置交易所/票池/窗口预热 cache，预热后离线）；④ `scripts/validate_hl.py`（真实 HL 小窗口 prewarm+回测，跑一次兑现需求 9）。

**Architecture:** 全部复用已迁移的纯函数：选币 `core.selection`（factors/select/compute_offset 金标同源）、布网 `core.grid_params`、仿真 `core.grid_engine.simulate_grid_engine`（与回测/实盘同源），数据经 P5a `DataSource`+`ParquetCache`。回测代码不含交易所差异（只经 adapter/datasource）。单测全程 FakeExchange/合成缓存离线；HL 验证是独立联网脚本（非 pytest 套件）。

**Tech Stack:** Python 3.9、pandas 1.3.5、ccxt 4.5.61、pytest。

> ⚠️ **铁律：不清楚的不要猜，一定要提问。** 实现中遇到任何不确定（持仓窗口对齐、聚合口径、HL 票池选取、本计划未写清处），必须停下来向用户提问确认，禁止用猜测继续实现。

## Global Constraints

- Python 3.9；pandas 1.3.5；datetime→ms 用 `.view('int64')//1_000_000`（勿对 datetime 用 astype）。
- `gridtrade/backtest/` 只经 `DataSource`/`ExchangeAdapter` 访问交易所，无 ccxt/交易所硬编码。
- 选币 point-in-time 截断口径与实盘一致：`(candle_begin_time + utc_offset) < run_time`，取最近 `max_candle_num` 根；复用 `core.selection.proceed_calc_symbol_factor` + `select_grid_coin` + `compute_offset`（金标同源，勿复制规则）。
- 持仓窗口 `holding_bars`：`[run_time, run_time+period)` 按 UTC+offset 墙钟对齐（移植自 `backtest/backtest_run.py:55-60`）。
- 仿真用 `core.grid_engine.simulate_grid_engine(neutral_init=True)`；资金费 `funding_df` 来自 cache（无则资金费止损不生效，标记 `funding_missing`）。
- timeframe 用 ccxt 统一小写 `'1h'`（cache namespace 同名）。
- 单测离线（FakeExchange seed_ohlcv / 合成缓存），无网络。HL 验证脚本联网、不进 pytest 套件。
- 不修改 `account_0/`、`backtest/`、`gridtrade/{core,exchanges,state,execution}/`、`gridtrade/backtest/{cache,datasource}.py`（本计划新增 selection_replay/backtest_run/prewarm + scripts/validate_hl.py）。
- 运行测试：`TZ=Asia/Shanghai .venv/bin/python -m pytest <args>`。

---

## 文件结构（本计划新建）

```
gridtrade/backtest/selection_replay.py   # point-in-time 选币回放（复用 core.selection）
gridtrade/backtest/prewarm.py            # 预热 DataSource 缓存
gridtrade/backtest/backtest_run.py       # 端到端回测 + summarize
scripts/__init__.py
scripts/validate_hl.py                   # 真实 Hyperliquid 验证（联网，手动跑一次）
tests/backtest/test_selection_replay.py
tests/backtest/test_prewarm.py
tests/backtest/test_backtest_run.py
```

---

### Task 1: selection_replay（point-in-time 选币回放）

**Files:**
- Create: `gridtrade/backtest/selection_replay.py`
- Create: `tests/backtest/test_selection_replay.py`

**Interfaces:**
- Consumes: `ParquetCache`（read_all_days）、`gridtrade.core.selection`（proceed_calc_symbol_factor / select_grid_coin / compute_offset）、`gridtrade.exchanges.base.CANDLE_COLS`。
- Produces: `gridtrade.backtest.selection_replay`：
  - `load_full_series(cache, symbols, timeframe='1h') -> dict[str, DataFrame]`：每 symbol `cache.read_all_days(timeframe, s)` → CANDLE_COLS、按 candle_begin_time 升序去重。
  - `replay_selection(cache, symbols, run_times, strategy_config, factors, utc_offset, on_select, *, timeframe='1h', log=print) -> int`：对每个 run_time 构造 point-in-time `symbol_candle_data`（cutoff + tail max_candle_num，最少 24 根），调 `proceed_calc_symbol_factor` + `select_grid_coin`，过滤当前周期，逐行 `on_select(run_time, offset, row)`；返回处理的 run_time 数。（移植自 `backtest/selection_replay.py`，仅把 import 改为 gridtrade.core、cache namespace 用 timeframe。）

- [ ] **Step 1: 写测试**

Create `tests/backtest/test_selection_replay.py`:

```python
import numpy as np
import pandas as pd

from gridtrade.backtest.cache import ParquetCache
from gridtrade.exchanges.base import CANDLE_COLS


def _bars(symbol, n=300, seed=0, start='2024-01-01'):
    rng = np.random.RandomState(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    t = pd.date_range(start, periods=n, freq='1H')
    return pd.DataFrame({
        'symbol': symbol, 'candle_begin_time': t,
        'open': open_, 'high': np.maximum(open_, close) * 1.001,
        'low': np.minimum(open_, close) * 0.999, 'close': close,
        'vol': rng.uniform(1e3, 1e4, n), 'volCcy': rng.uniform(1e3, 1e4, n),
        'quote_volume': rng.uniform(1e6, 1e7, n),
    })[CANDLE_COLS]


def _seed_cache(tmp_path, symbols):
    cache = ParquetCache(str(tmp_path))
    for i, s in enumerate(symbols):
        df = _bars(s, seed=i + 1)
        for day, g in df.groupby(df['candle_begin_time'].dt.strftime('%Y-%m-%d')):
            cache.write('1h', s, day, g.reset_index(drop=True))
    return cache


STRAT = {'period': '12H', 'weight_list': [1, 1, 1], 'choose_symbols': 1,
         'max_candle_num': 160}
FACTORS = {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True}


def test_load_full_series(tmp_path):
    from gridtrade.backtest.selection_replay import load_full_series
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    series = load_full_series(cache, syms, timeframe='1h')
    assert set(series) == set(syms)
    assert list(series['AAA/USDT:USDT'].columns) == CANDLE_COLS
    assert series['AAA/USDT:USDT']['candle_begin_time'].is_monotonic_increasing


def test_replay_selection_emits_picks(tmp_path):
    from gridtrade.backtest.selection_replay import replay_selection
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    run_times = [pd.Timestamp('2024-01-10 00:00:00'), pd.Timestamp('2024-01-10 12:00:00')]
    picks = []
    n = replay_selection(cache, syms, run_times, STRAT, FACTORS, 8,
                         lambda rt, off, row: picks.append((rt, off, row['symbol'])),
                         timeframe='1h')
    assert n == 2
    assert len(picks) >= 1                     # 至少选出一个币
    # 每个 pick 的 row 含布网所需列
    assert all(isinstance(p[2], str) for p in picks)
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_selection_replay.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.backtest.selection_replay`）。

- [ ] **Step 3: 写 selection_replay.py**

Create `gridtrade/backtest/selection_replay.py`:

```python
"""选币回放（Live/Backtest parity + point-in-time）。复用 gridtrade.core.selection 的实盘选币纯函数。
构造每个 run_time 的 symbol_candle_data 时严格只用 (candle_begin_time + utc_offset) < run_time 的 bar、
取最近 max_candle_num 根，与实盘截断口径一致。
"""
import contextlib
import os
import time

import pandas as pd

from gridtrade.core.selection import (compute_offset, proceed_calc_symbol_factor,
                                      select_grid_coin)
from gridtrade.exchanges.base import CANDLE_COLS


def load_full_series(cache, symbols, timeframe='1h'):
    series = {}
    for s in symbols:
        df = cache.read_all_days(timeframe, s)
        if df is None or df.empty:
            continue
        df = df[CANDLE_COLS].copy()
        df.sort_values('candle_begin_time', inplace=True)
        df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)
        df.reset_index(drop=True, inplace=True)
        series[s] = df
    return series


def replay_selection(cache, symbols, run_times, strategy_config, factors,
                     utc_offset, on_select, *, timeframe='1h', log=print):
    period = strategy_config['period']
    weight_list = strategy_config['weight_list']
    choose_symbols = strategy_config['choose_symbols']
    max_candle_num = strategy_config['max_candle_num']
    if len(weight_list) != len(factors):
        log('[SR][WARN] weight_list(%d)!=factors(%d), 用等权' % (len(weight_list), len(factors)))
        weight_list = [1] * len(factors)

    series = load_full_series(cache, symbols, timeframe)
    processed = 0
    devnull = open(os.devnull, 'w')
    try:
        for run_time in run_times:
            run_time = pd.Timestamp(run_time)
            offset = compute_offset(run_time, period, utc_offset)
            symbol_candle_data = {}
            for s, df in series.items():
                mask = (df['candle_begin_time'] + pd.Timedelta(hours=utc_offset)) < run_time
                sub = df[mask]
                if len(sub) < 24:
                    continue
                symbol_candle_data[s] = sub.tail(max_candle_num).copy()
            if not symbol_candle_data:
                processed += 1
                continue
            with contextlib.redirect_stdout(devnull):
                all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset)
                if all_df is None or all_df.empty:
                    processed += 1
                    continue
                factor_data = select_grid_coin(all_df, factors, weight_list, choose_symbols, run_time)
            factor_data = factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= run_time]
            for _, row in factor_data.iterrows():
                on_select(run_time, offset, row)
            processed += 1
    finally:
        devnull.close()
    return processed
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_selection_replay.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/selection_replay.py tests/backtest/test_selection_replay.py
git commit -m "feat(backtest): point-in-time selection_replay reusing core.selection"
```

---

### Task 2: backtest_run（端到端回测 + summarize）

**Files:**
- Create: `gridtrade/backtest/backtest_run.py`
- Create: `tests/backtest/test_backtest_run.py`

**Interfaces:**
- Consumes: `selection_replay.replay_selection/load_full_series`、`core.grid_params.calc_grid_params_v1/v2`、`core.grid_engine.simulate_grid_engine`、`ParquetCache`。
- Produces: `gridtrade.backtest.backtest_run`：
  - `holding_bars(series_df, run_time, period, utc_offset) -> DataFrame`（移植自旧版 55-60 行）。
  - `summarize(df) -> dict`（移植自旧版 111-130：n_grids/win_rate/mean/median pnl_ratio/portfolio_return[按 offset 复利等权]/exit_reasons）。
  - `run_backtest(cache, universe, window_start, window_end, strategy_config, factors, utc_offset, *, timeframe='1h', fee_rate=0.0005, max_rate=0.5, leverage=None, log=print) -> DataFrame`：replay→逐格 holding_bars→calc_grid_params(v1/v2 by grid_version)→simulate_grid_engine(cap=1000, neutral_init, stop_cfg, funding_df=cache.read_all_days('funding',sym))→收集 {run_time,offset,symbol,entry,grid_num,low,high,hold_bars,n_fills,pnl_ratio,exit_reason,terminated,funding_missing}。leverage 默认取 strategy_config['leverage']。

- [ ] **Step 1: 写测试**

Create `tests/backtest/test_backtest_run.py`:

```python
import pandas as pd

from tests.backtest.test_selection_replay import _seed_cache, STRAT, FACTORS


def _strategy():
    return dict(STRAT, leverage=5, price_limit=[0.25, 0.25], stop_limit=0.01,
                grid_version=2,
                grid_v2_config={'atr_range_multiplier': 3, 'range_pct_min': 0.05,
                                'range_pct_max': 0.25, 'grid_spacing_atr_ratio': 0.5,
                                'grid_spacing_min': 0.003, 'grid_spacing_max': 0.02,
                                'grid_count_min': 25, 'grid_count_max': 149,
                                'stop_buffer_ratio': 0.01},
                stop_loss_config={'stop_loss': 0.034, 'trailing_k': 0.3,
                                  'trailing_floor': 0.00618, 'fundingRate_stop_loss': 0.0015})


def test_holding_bars_window(tmp_path):
    from gridtrade.backtest.backtest_run import holding_bars
    from gridtrade.backtest.selection_replay import load_full_series
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    series = load_full_series(cache, syms, '1h')
    sub = holding_bars(series['AAA/USDT:USDT'], pd.Timestamp('2024-01-05 00:00:00'), '12H', 8)
    # 12H 窗口（UTC+8 对齐）应有约 12 根 1h bar
    assert 1 <= len(sub) <= 13


def test_summarize_shape():
    from gridtrade.backtest.backtest_run import summarize
    df = pd.DataFrame({'run_time': pd.to_datetime(['2024-01-01', '2024-01-01']),
                       'offset': [0, 1], 'pnl_ratio': [0.02, -0.01],
                       'exit_reason': ['窗口结束', '固定止损']})
    s = summarize(df)
    assert s['n_grids'] == 2 and 0.0 <= s['win_rate'] <= 1.0
    assert 'portfolio_return' in s and 'exit_reasons' in s


def test_summarize_empty():
    from gridtrade.backtest.backtest_run import summarize
    assert summarize(pd.DataFrame())['n_grids'] == 0


def test_run_backtest_end_to_end(tmp_path):
    from gridtrade.backtest.backtest_run import run_backtest
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    df = run_backtest(cache, syms, pd.Timestamp('2024-01-10 00:00:00'),
                      pd.Timestamp('2024-01-11 00:00:00'), _strategy(), FACTORS, 8,
                      timeframe='1h')
    # 至少跑出结果行，列齐全
    assert set(['run_time', 'offset', 'symbol', 'pnl_ratio', 'exit_reason',
                'grid_num', 'hold_bars']).issubset(df.columns)
    if not df.empty:
        assert df['pnl_ratio'].notna().all()
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_run.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.backtest.backtest_run`）。

- [ ] **Step 3: 写 backtest_run.py**

Create `gridtrade/backtest/backtest_run.py`:

```python
"""端到端回测：选币回放 → 布网 → 持仓 bars → simulate_grid_engine → 聚合。
全部复用 gridtrade.core 纯函数；数据从 ParquetCache 读（预热后离线）。
"""
import pandas as pd

from gridtrade.backtest import selection_replay as SR
from gridtrade.core.grid_engine import simulate_grid_engine
from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2


def holding_bars(series_df, run_time, period, utc_offset):
    td = pd.to_timedelta(period)
    local_t = series_df['candle_begin_time'] + pd.Timedelta(hours=utc_offset)
    sub = series_df[(local_t >= run_time) & (local_t < run_time + td)]
    return sub.sort_values('candle_begin_time')


def _funding_missing(funding_df, bars_df):
    if funding_df is None or funding_df.empty or len(bars_df) == 0:
        return True
    lo = bars_df['candle_begin_time'].min()
    hi = bars_df['candle_begin_time'].max()
    fts = pd.to_datetime(funding_df['ts'], unit='ms')
    return not ((fts >= lo) & (fts <= hi)).any()


def summarize(df):
    if df.empty:
        return {'n_grids': 0}
    offset_eq = {}
    for off, g in df.sort_values('run_time').groupby('offset'):
        eq = 1.0
        for pr in g['pnl_ratio']:
            eq *= (1.0 + pr)
        offset_eq[int(off)] = eq
    port_return = sum(offset_eq.values()) / len(offset_eq) - 1.0
    return {
        'n_grids': int(len(df)),
        'win_rate': float((df['pnl_ratio'] > 0).mean()),
        'mean_pnl_ratio': float(df['pnl_ratio'].mean()),
        'median_pnl_ratio': float(df['pnl_ratio'].median()),
        'portfolio_return': float(port_return),
        'offset_equity': offset_eq,
        'exit_reasons': df['exit_reason'].value_counts().to_dict(),
    }


def run_backtest(cache, universe, window_start, window_end, strategy_config, factors,
                 utc_offset, *, timeframe='1h', fee_rate=0.0005, max_rate=0.5,
                 leverage=None, log=print):
    period = strategy_config['period']
    price_limit = strategy_config['price_limit']
    stop_limit = strategy_config['stop_limit']
    lev = leverage if leverage is not None else strategy_config['leverage']
    grid_version = strategy_config.get('grid_version', 1)
    v2cfg = strategy_config.get('grid_v2_config', {})
    stop_cfg = strategy_config['stop_loss_config']
    calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1

    series = SR.load_full_series(cache, universe, timeframe)
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors, utc_offset,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, log=log)
    log('[BT] picks=%d' % len(grids))

    results = []
    for rt, offset, row in grids:
        sym = row['symbol']
        if sym not in series:
            continue
        bars_df = holding_bars(series[sym], rt, period, utc_offset)
        if len(bars_df) == 0:
            continue
        px = calc_fn(row=row, price_limit=price_limit, stop_limit=stop_limit, v2_config=v2cfg)
        gp = dict(low_price=px['low_price'], high_price=px['high_price'],
                  grid_count=px['grid_count'], stop_high_price=px['stop_high_price'],
                  stop_low_price=px['stop_low_price'])
        funding_df = cache.read_all_days('funding', sym)
        sim = simulate_grid_engine(bars_df, gp, cap=1000.0, leverage=lev, fee=fee_rate,
                                   max_rate=max_rate, min_amount=0.0, stop_cfg=stop_cfg,
                                   funding_df=funding_df)
        results.append({
            'run_time': rt, 'offset': int(offset), 'symbol': sym,
            'entry': float(row['close']), 'grid_num': int(px['grid_count']),
            'low': round(px['low_price'], 8), 'high': round(px['high_price'], 8),
            'hold_bars': int(len(bars_df)), 'n_fills': int(sim['n_trades']),
            'pnl_ratio': float(sim['pnl_ratio']), 'exit_reason': sim['exit_reason'],
            'terminated': bool(sim['terminated']),
            'funding_missing': bool(_funding_missing(funding_df, bars_df)),
        })
    return pd.DataFrame(results)
```

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_run.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/backtest_run.py tests/backtest/test_backtest_run.py
git commit -m "feat(backtest): end-to-end run_backtest + summarize (reuse core engine/params)"
```

---

### Task 3: prewarm（按配置交易所预热缓存）

**Files:**
- Create: `gridtrade/backtest/prewarm.py`
- Create: `tests/backtest/test_prewarm.py`

**Interfaces:**
- Consumes: `DataSource`（fetch_ohlcv_range / fetch_funding_range / list_instruments）。
- Produces: `gridtrade.backtest.prewarm`：
  - `prewarm_ohlcv(datasource, universe, start_ms, end_ms, *, log=print) -> dict`：对每 symbol `datasource.fetch_ohlcv_range(s, start_ms, end_ms)`（写满 per-day cache），返回 `{'symbols': n, 'rows': total_rows}`。
  - `prewarm_funding(datasource, universe, start_ms, end_ms, *, log=print) -> dict`：同理 `fetch_funding_range`。
  - `resolve_universe(datasource, *, quote='USDT', min_list_age_days=15, limit=None) -> list[str]`：`datasource.list_instruments()` 过滤 `state=='live'`、上市≥min_list_age_days（用 list_ts；若为 0 视为通过）、规范符号（含 `/USDT:USDT` 或由 adapter 规范化）；可选 `limit` 截断。返回规范符号列表。

- [ ] **Step 1: 写测试**

Create `tests/backtest/test_prewarm.py`:

```python
import pandas as pd

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument, CANDLE_COLS

SYMS = ['AAA/USDT:USDT', 'BBB/USDT:USDT']


def _bars(symbol, start_ms, n):
    ts = [start_ms + i * 3600_000 for i in range(n)]
    return pd.DataFrame({'symbol': symbol, 'candle_begin_time': pd.to_datetime(ts, unit='ms'),
                         'open': [1.0] * n, 'high': [2.0] * n, 'low': [0.5] * n,
                         'close': [1.5] * n, 'vol': [9.0] * n, 'volCcy': [9.0] * n,
                         'quote_volume': [13.0] * n})[CANDLE_COLS]


def _ds(tmp_path, ex):
    from gridtrade.backtest.cache import ParquetCache
    from gridtrade.backtest.datasource import DataSource
    return DataSource(ex, ParquetCache(str(tmp_path)), timeframe='1h')


def test_prewarm_ohlcv_populates_cache_then_offline(tmp_path):
    from gridtrade.backtest.prewarm import prewarm_ohlcv
    start = 1_704_067_200_000
    ex = FakeExchange(instruments=[Instrument(s, 0.1, 0.001, 0.001, 'live', 0) for s in SYMS])
    for s in SYMS:
        ex.seed_ohlcv(s, _bars(s, start, 48))
    ds = _ds(tmp_path, ex)
    stat = prewarm_ohlcv(ds, SYMS, start, start + 47 * 3600_000)
    assert stat['symbols'] == 2 and stat['rows'] == 96

    # 预热后离线：用会报错的交易所，仅靠缓存仍取得
    class Offline(FakeExchange):
        def fetch_ohlcv(self, *a, **k):
            raise AssertionError('should be offline after prewarm')
    off = Offline(instruments=[Instrument(s, 0.1, 0.001, 0.001, 'live', 0) for s in SYMS])
    ds2 = _ds(tmp_path, off)
    df = ds2.fetch_ohlcv_range('AAA/USDT:USDT', start, start + 47 * 3600_000)
    assert len(df) == 48


def test_resolve_universe_filters(tmp_path):
    from gridtrade.backtest.prewarm import resolve_universe
    insts = [Instrument('AAA/USDT:USDT', 0.1, 0.001, 0.001, 'live', 0),
             Instrument('BBB/USDT:USDT', 0.1, 0.001, 0.001, 'expired', 0),
             Instrument('CCC/USDT:USDT', 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts)
    ds = _ds(tmp_path, ex)
    uni = resolve_universe(ds)
    assert 'AAA/USDT:USDT' in uni and 'CCC/USDT:USDT' in uni
    assert 'BBB/USDT:USDT' not in uni            # 非 live 过滤掉
    assert len(resolve_universe(ds, limit=1)) == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_prewarm.py -v`
Expected: FAIL（`ModuleNotFoundError: gridtrade.backtest.prewarm`）。

- [ ] **Step 3: 写 prewarm.py**

Create `gridtrade/backtest/prewarm.py`:

```python
"""预热：按配置交易所/票池/窗口把 DataSource 缓存填满，预热后回测离线。"""


def resolve_universe(datasource, *, quote='USDT', min_list_age_days=15, limit=None):
    out = []
    for inst in datasource.list_instruments():
        if inst.state != 'live':
            continue
        # list_ts==0 视为未知，放行；否则可按需扩展上市时长过滤（此处保留接口）
        sym = inst.symbol
        if (':%s' % quote) in sym or ('/%s:' % quote) in sym or sym.endswith('/%s' % quote):
            out.append(sym)
        else:
            out.append(sym)  # 规范符号已由 adapter 统一；不强制 quote 形态
    out = sorted(set(out))
    return out[:limit] if limit else out


def prewarm_ohlcv(datasource, universe, start_ms, end_ms, *, log=print):
    total = 0
    n = 0
    for s in universe:
        df = datasource.fetch_ohlcv_range(s, start_ms, end_ms)
        total += int(len(df))
        n += 1
        if n % 25 == 0:
            log('[prewarm] ohlcv %d/%d' % (n, len(universe)))
    return {'symbols': n, 'rows': total}


def prewarm_funding(datasource, universe, start_ms, end_ms, *, log=print):
    total = 0
    n = 0
    for s in universe:
        df = datasource.fetch_funding_range(s, start_ms, end_ms)
        total += int(len(df))
        n += 1
    return {'symbols': n, 'rows': total}
```

> 注：`resolve_universe` 的 quote 过滤对已规范化符号放宽（adapter 已统一为 `BASE/USDT:USDT`）；list_age 过滤留接口、默认放行（list_ts 多为 0/未知）。如需严格上市时长过滤，需 adapter 提供可靠 list_ts——不确定则问。

- [ ] **Step 4: 运行确认通过**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_prewarm.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 全套回归 + 提交**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全 PASS（既有 111 + 本计划新增 ≈ 8）。

```bash
git add gridtrade/backtest/prewarm.py tests/backtest/test_prewarm.py
git commit -m "feat(backtest): prewarm (populate DataSource cache, offline after warm)"
```

---

### Task 4: Hyperliquid 真实验证脚本 + 跑一次（需求 9）

**Files:**
- Create: `scripts/__init__.py`, `scripts/validate_hl.py`

**Interfaces:**
- Produces: `scripts/validate_hl.py`：用 `HyperliquidAdapter`（真实 ccxt）+ `DataSource` 对小票池小窗口 prewarm（联网一次）+ `run_backtest`（离线）→ 打印汇总。**联网脚本，不进 pytest 套件。**

- [ ] **Step 1: 写脚本**

Create `scripts/__init__.py`（空）。

Create `scripts/validate_hl.py`:

```python
"""真实 Hyperliquid 端到端验证（需求 9）：联网小窗口 prewarm + 离线回测。
跑：TZ=Asia/Shanghai .venv/bin/python scripts/validate_hl.py
注：联网、耗时；非 pytest 套件。证明同一份回测代码经配置即可在 HL 上拉数回测。
"""
import os
import sys
import time

import ccxt
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.datasource import DataSource
from gridtrade.backtest import prewarm as PW
from gridtrade.backtest.backtest_run import run_backtest, summarize

UNIVERSE = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'AVAX/USDT:USDT',
            'ARB/USDT:USDT', 'OP/USDT:USDT', 'LINK/USDT:USDT', 'DOGE/USDT:USDT']

STRATEGY = {
    'period': '12H', 'max_candle_num': 160, 'weight_list': [1, 1, 1],
    'choose_symbols': 1, 'leverage': 5, 'price_limit': [0.25, 0.25], 'stop_limit': 0.01,
    'grid_version': 2,
    'grid_v2_config': {'atr_range_multiplier': 3, 'range_pct_min': 0.05, 'range_pct_max': 0.25,
                       'grid_spacing_atr_ratio': 0.5, 'grid_spacing_min': 0.003,
                       'grid_spacing_max': 0.02, 'grid_count_min': 25, 'grid_count_max': 149,
                       'stop_buffer_ratio': 0.01},
    'stop_loss_config': {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
                         'fundingRate_stop_loss': 0.0015},
}
FACTORS = {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True}


def main():
    cache = ParquetCache(os.path.join(os.path.dirname(__file__), '..', 'data', 'hl_validate'))
    adapter = HyperliquidAdapter(ccxt.hyperliquid({'enableRateLimit': True, 'timeout': 30000}))
    ds = DataSource(adapter, cache, timeframe='1h')

    # 窗口：最近 ~10 天（+暖机）。实时验证脚本，用本机时钟作锚（非确定性测试，可接受）。
    one_h = 3600_000
    end_ms = int(time.time() * 1000)
    warm_start = end_ms - 22 * 24 * one_h     # 含暖机
    win_start = end_ms - 10 * 24 * one_h
    print('[HL] window end=%s start=%s' % (pd.to_datetime(end_ms, unit='ms'),
                                           pd.to_datetime(win_start, unit='ms')))

    t0 = time.time()
    stat = PW.prewarm_ohlcv(ds, UNIVERSE, warm_start, end_ms)
    print('[HL] prewarm ohlcv:', stat, '%.1fs' % (time.time() - t0))

    df = run_backtest(cache, UNIVERSE, pd.to_datetime(win_start, unit='ms'),
                      pd.to_datetime(end_ms, unit='ms'), STRATEGY, FACTORS, utc_offset=0,
                      timeframe='1h')
    print('\n===== HL 回测汇总 =====')
    for k, v in summarize(df).items():
        print('  %s: %s' % (k, v))
    if not df.empty:
        print('\n样本:\n', df.head(10).to_string(index=False))
    print('\n[HL] 验证完成：同一回测代码经配置在 Hyperliquid 上拉数+回测成功。')


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: 跑一次（联网）**

Run: `TZ=Asia/Shanghai .venv/bin/python scripts/validate_hl.py`
Expected: 打印窗口、prewarm 统计、HL 回测汇总（n_grids、win_rate、portfolio_return、exit_reasons），最后 "验证完成"。**这步联网、可能数十秒**；若 HL 限频/超时，重试或缩小 UNIVERSE/窗口。

> 若本环境无网络或 HL 不可达：把实际报错贴出来并停下，与用户确认（不要伪造结果）。验证脚本不进 pytest 套件，不影响离线测试全绿。

- [ ] **Step 3: 记录结果并提交**

把脚本输出的关键汇总（n_grids/portfolio_return/exit_reasons）粘进 commit message 或 report，证明需求 9 达成。

```bash
git add scripts/__init__.py scripts/validate_hl.py
git commit -m "feat(scripts): real Hyperliquid end-to-end backtest validation (req 9)"
```

- [ ] **Step 4: 全套回归确认离线套件不受影响**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest -q`
Expected: 全 PASS（scripts/ 不在 testpaths，离线套件不变）。

---

## 完成判定（P5b）

- `pytest -q` 全绿：selection_replay 选币回放、backtest_run 端到端（holding/summarize/run_backtest）、prewarm 预热后离线、resolve_universe 过滤。
- `scripts/validate_hl.py` 真实跑通：HL 联网 prewarm + 离线回测出汇总（兑现需求 9：同一回测代码经配置在 Hyperliquid 上拉数回测）。
- `gridtrade/backtest/` 只经 DataSource/adapter，无交易所硬编码。

## 后续

至此需求 7（按配置交易所动态拉数回测）、8（预热后离线）、9（HL 验证）达成，P5 完成。剩余 **P4 运行时/fly.io**（需用户基础设施决策）、P6 加固、P7 同币种多网格。P5b follow-up（spec 记录）：quote_volume 真实成交额映射、funding-range 离线测试、read_all_days 空哨兵——可在 P6 或按需补。
