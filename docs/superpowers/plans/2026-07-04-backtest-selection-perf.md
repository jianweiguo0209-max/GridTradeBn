# 回测选币性能优化 + selection.py warning 修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把回测选币（`replay_selection`/`select_grids`）从单核串行改为多进程并行（复用 `BT_WORKERS`）、给选币结果加磁盘缓存（同窗口秒回），并修掉 `core/selection.py` 两个 warning —— 全程与串行逐位一致、金标 parity 保持绿。

**Architecture:** 三块相互独立、可分别验收。① `core/selection.py` 两处最小改动修 warning（金标 + base↔offset 等价测双护栏）。② `selection_replay.py` 抽纯循环体 `_select_over_run_times` + 顶层可 pickle worker `_replay_chunk`，`replay_selection` 加 `workers` 参数走 `ProcessPoolExecutor.map`（连续切块 + map 保序 ⇒ 逐位一致）。③ 新 `select_cache.py`（key=选币参数+每币缓存天范围数据指纹，pickle 落盘，version/params 双校验）在 `select_grids` 层收口。

**Tech Stack:** Python 3.9 / pandas 1.3.5 / numpy 1.22.4 / pyarrow；并行用标准库 `concurrent.futures`；缓存用标准库 `hashlib`/`json`/`pickle`。不新增第三方依赖。

## Global Constraints

- 依赖冻结：Python 3.9 / pandas 1.3.5 / numpy 1.22.4 / pyarrow；不新增第三方库。
- 金标 parity 不可破：`tests/core/test_selection_parity.py`、`tests/core/test_factors_parity.py` 改动后逐位绿（锁返回值、非 stdout）。
- 并行/缓存结果与串行**逐位一致**：`select_grids(workers=1)` == `workers=N`，缓存 MISS == HIT。
- 新增参数一律默认关/向后兼容：`workers=1`；缓存默认开、`BT_SELECT_CACHE=off` 旁路。旧调用签名不破。
- `core/selection.py` 只做两处 warning 的最小改动，不碰选币逻辑 / 因子算法 / 网格参数。
- 不并行网络预热、不动 `simulate_tasks`、不碰 prod / production 分支。
- 测试命令：`TZ=Asia/Shanghai .venv/bin/python -m pytest`（单文件加 `<路径> -v`）。
- `core/selection.py` 里 `proceed_calc_symbol_factor` 的 `no data`/`no enough data`/`[警告]…` 三处 print **保留**（合法诊断，由 `replay_selection` 的 `redirect_stdout` 抑制）。

---

### Task 1: 修 `core/selection.py` 两个 warning（base→offset + 删 debug 块）

**Files:**
- Modify: `gridtrade/core/selection.py:32`（`base=offset` → `offset=`）、`gridtrade/core/selection.py:129-135`（删 debug 块）
- Modify: `pyproject.toml:5-8`（删两条已失效的 filterwarnings ignore）
- Test: `tests/core/test_selection_warnings.py`（新）

**Interfaces:**
- Consumes: `gridtrade.core.selection.proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset)`、`select_grid_coin(data, factor_info, weight_list, choose_symbols, run_time)`；`tests.golden.gen_golden.make_symbol_df(symbol, n, seed)`。
- Produces: 无新公共接口（纯修复）；`selection.py` 行为字节等价（金标锁定）。

- [ ] **Step 1: 写等价 + 无告警测试（先失败）**

Create `tests/core/test_selection_warnings.py`:

```python
import warnings

import numpy as np
import pandas as pd


def test_resample_base_offset_equivalent():
    """证明 base=k（小时）≡ offset=Timedelta(hours=k)，全相位 0..11（base→offset 迁移的安全性依据）。"""
    t = pd.date_range('2024-01-01 00:00:00', periods=240, freq='1H')
    df = pd.DataFrame({'x': np.arange(240, dtype='float64')}, index=t)
    for k in range(12):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', FutureWarning)   # 老写法刻意触发，仅作对比基准
            old = df.resample('12H', base=k).sum()
        new = df.resample('12H', offset=pd.Timedelta(hours=k)).sum()
        pd.testing.assert_frame_equal(old, new)


def test_selection_path_emits_no_target_warnings():
    """选币路径（含 offset≠0）不得再冒 base= FutureWarning / SettingWithCopyWarning。"""
    from tests.golden.gen_golden import make_symbol_df
    from gridtrade.core.selection import proceed_calc_symbol_factor, select_grid_coin
    period, offset = '12H', 3                              # 非零相位，走新 offset= 路径
    run_time = pd.Timestamp('2024-01-09 00:00:00')
    symbols = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    scd = {s: make_symbol_df(s, n=240, seed=i + 10) for i, s in enumerate(symbols)}
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter('always')                   # 本地覆盖 pyproject 的全局 ignore
        all_df = proceed_calc_symbol_factor(scd, run_time, period, offset)
        select_grid_coin(all_df.copy(), {'Reg_v2_5': True, 'Sgcz_5': True, 'Er_2': True},
                         [1, 1, 1], 2, run_time)
    msgs = [str(w.message) for w in rec]
    names = [type(w.message).__name__ for w in rec]
    assert not any("'base' in" in m for m in msgs), msgs
    assert 'SettingWithCopyWarning' not in names, names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/core/test_selection_warnings.py -v`
Expected: `test_resample_base_offset_equivalent` PASS（纯 pandas 行为）；`test_selection_path_emits_no_target_warnings` **FAIL**（当前 debug 块冒 SettingWithCopyWarning、`base=` 冒 FutureWarning）。

- [ ] **Step 3: 改 `selection.py:32` base→offset**

将 `gridtrade/core/selection.py` 第 32 行：
```python
    period_df = data.resample(rule=period, base=offset).agg(agg_dict)
```
改为：
```python
    period_df = data.resample(rule=period, offset=pd.Timedelta(hours=offset)).agg(agg_dict)
```

- [ ] **Step 4: 删 `selection.py` debug 块（第 129-135 行）**

删除 `select_grid_coin` 内这一整块（注释行到 `# exit()`）：
```python
    # 测试用：打印当前周期的全集排序
    pdata = data[(data['time'] + pd.to_timedelta('12H')) >= run_time]
    pdata.sort_values(by='rank', inplace=True)
    pdata["time"] = pdata["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    print("当前周期的全集选币排序")
    print(pdata.head(10))
    # exit()
```
删除后，上文 `data['rank'] = data.groupby('time')['rank_sum'].rank(...)` 与下文 `# 选币` / `data = data[data['rank'] <= choose_symbols]` 直接相邻。**不动** `data` 其它逻辑。

- [ ] **Step 5: 删 `pyproject.toml` 两条失效 ignore**

将 `pyproject.toml` 的：
```toml
filterwarnings = [
    "ignore:.*'base' in \\.resample.*:FutureWarning",
    "ignore::pandas.core.common.SettingWithCopyWarning",
]
```
整段删除（这两条针对的 warning 已从代码根除，留着会掩盖回归）。

- [ ] **Step 6: 跑新测试 + 金标 + 全套**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/core/test_selection_warnings.py tests/core/test_selection_parity.py tests/core/test_factors_parity.py -v`
Expected: 全 PASS（等价测 + 无告警测 + 金标逐位不漂）。

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全套绿（删 ignore 后无新失败；`addopts=-q` 下即便有无关 warning 也只打印不失败）。

- [ ] **Step 7: 提交**

```bash
git add gridtrade/core/selection.py pyproject.toml tests/core/test_selection_warnings.py
git commit -m "fix(selection): 修 base= FutureWarning + 删 debug-print SettingWithCopyWarning

base=offset→offset=Timedelta(hours=offset)（全相位等价+金标不漂）；删遗留 debug 块（消告警+prod日志噪声+格式化开销）；清 pyproject 两条已失效 filterwarnings ignore。"
```

---

### Task 2: `selection_replay.py` 选币多进程并行

**Files:**
- Modify: `gridtrade/backtest/selection_replay.py`（抽 `_select_over_run_times`、加 `_replay_chunk`/`_split_contiguous`、`replay_selection` 加 `workers`）
- Test: `tests/backtest/test_selection_replay.py`（加并行 parity 测）

**Interfaces:**
- Consumes: `load_full_series(cache, symbols, timeframe)`、`build_pit_candidates(...)`、`compute_offset`、`proceed_calc_symbol_factor`、`select_grid_coin`（均本文件/`core.selection` 已有）。`ParquetCache`（只含 `root` 字符串 → 可 pickle）。
- Produces: `replay_selection(cache, symbols, run_times, strategy_config, factors, on_select, *, timeframe='1h', min_quote_volume=0.0, blacklist=(), workers=1, log=print) -> int`（新增 `workers`，默认 1 行为不变）。顶层 `_replay_chunk(payload)`、`_select_over_run_times(...)`、`_split_contiguous(items, n)`。

- [ ] **Step 1: 写并行 parity 测（先失败）**

在 `tests/backtest/test_selection_replay.py` 末尾追加：
```python
def test_replay_selection_parallel_matches_serial(tmp_path):
    from gridtrade.backtest.selection_replay import replay_selection
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    run_times = list(pd.date_range('2024-01-09', '2024-01-12', freq='12H'))   # 7 个 → 多 chunk

    def collect(w):
        picks = []
        replay_selection(cache, syms, run_times, STRAT, FACTORS,
                         lambda rt, off, row: picks.append(
                             (str(rt), int(off), row['symbol'], round(float(row['close']), 8))),
                         timeframe='1h', workers=w)
        return picks

    serial = collect(1)
    par = collect(3)
    assert len(serial) > 0
    assert serial == par                 # 逐条完全一致
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_selection_replay.py::test_replay_selection_parallel_matches_serial -v`
Expected: FAIL —— `replay_selection() got an unexpected keyword argument 'workers'`。

- [ ] **Step 3: 抽循环体 + 加 worker/split + 改 `replay_selection`**

将 `gridtrade/backtest/selection_replay.py` 的 `replay_selection`（第 49-84 行）整段替换为下列三个新函数 + 改写后的 `replay_selection`（其余 import/`load_full_series`/`build_pit_candidates` 不动；文件已 `import contextlib, os`，已 `import pandas as pd`）：

```python
def _select_over_run_times(series, run_times, period, weight_list, factors,
                           choose_symbols, max_candle_num, min_quote_volume, blacklist):
    """逐 run_time 选币的纯循环体（串行/并行共用）。返回 [(run_time, offset, row)]。
    内部 redirect_stdout 抑制 core 选币函数的诊断 print（no data/[警告] 等）。"""
    out = []
    devnull = open(os.devnull, 'w')
    try:
        for run_time in run_times:
            run_time = pd.Timestamp(run_time)
            offset = compute_offset(run_time, period)
            symbol_candle_data = build_pit_candidates(
                series, run_time, max_candle_num=max_candle_num,
                min_quote_volume=min_quote_volume, blacklist=blacklist)
            if not symbol_candle_data:
                continue
            with contextlib.redirect_stdout(devnull):
                all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period, offset)
                if all_df is None or all_df.empty:
                    continue
                factor_data = select_grid_coin(all_df, factors, weight_list, choose_symbols, run_time)
            factor_data = factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= run_time]
            for _, row in factor_data.iterrows():
                out.append((run_time, offset, row.copy()))
    finally:
        devnull.close()
    return out


def _split_contiguous(items, n):
    """把有序列表切成 n 段连续、近等长的子列表（保序；空段丢弃）。"""
    if not items:
        return []
    n = max(1, min(n, len(items)))
    k, m = divmod(len(items), n)
    out, i = [], 0
    for j in range(n):
        sz = k + (1 if j < m else 0)
        if sz:
            out.append(items[i:i + sz])
        i += sz
    return out


def _replay_chunk(payload):
    """进程池 worker（顶层、可 pickle）：各自从本地缓存载 series 后选自己那段 run_time。"""
    (cache, symbols, run_times_chunk, timeframe, period, weight_list, factors,
     choose_symbols, max_candle_num, min_quote_volume, blacklist) = payload
    series = load_full_series(cache, symbols, timeframe)
    return _select_over_run_times(series, run_times_chunk, period, weight_list, factors,
                                  choose_symbols, max_candle_num, min_quote_volume, blacklist)


def replay_selection(cache, symbols, run_times, strategy_config, factors, on_select, *,
                     timeframe='1h', min_quote_volume=0.0, blacklist=(), workers=1, log=print):
    period = strategy_config['period']
    weight_list = strategy_config['weight_list']
    choose_symbols = strategy_config['choose_symbols']
    max_candle_num = strategy_config['max_candle_num']
    if len(weight_list) != len(factors):
        log('[SR][WARN] weight_list(%d)!=factors(%d), 用等权' % (len(weight_list), len(factors)))
        weight_list = [1] * len(factors)

    run_times = list(run_times)
    if workers and workers > 1 and len(run_times) > 1:
        from concurrent.futures import ProcessPoolExecutor
        chunks = _split_contiguous(run_times, workers)
        payloads = [(cache, symbols, chunk, timeframe, period, weight_list, factors,
                     choose_symbols, max_candle_num, min_quote_volume, blacklist)
                    for chunk in chunks]
        with ProcessPoolExecutor(max_workers=len(payloads)) as ex:
            for chunk_result in ex.map(_replay_chunk, payloads):   # map 保输入序 ⇒ 与串行逐位一致
                for run_time, offset, row in chunk_result:
                    on_select(run_time, offset, row)
    else:
        series = load_full_series(cache, symbols, timeframe)
        for run_time, offset, row in _select_over_run_times(
                series, run_times, period, weight_list, factors,
                choose_symbols, max_candle_num, min_quote_volume, blacklist):
            on_select(run_time, offset, row)
    return len(run_times)
```

- [ ] **Step 4: 跑并行测 + 现有回放测（回归）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_selection_replay.py -v`
Expected: 全 PASS（新并行 parity 绿；旧 `test_replay_selection_emits_picks`/`test_load_full_series`/`test_build_pit_candidates_*` 仍绿 = 串行行为不变）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/selection_replay.py tests/backtest/test_selection_replay.py
git commit -m "feat(backtest): 选币回放多进程并行（replay_selection workers）

抽纯循环体 _select_over_run_times（串行/并行共用）+ 顶层可 pickle worker _replay_chunk（各 worker 本地重载 series）；连续切块 + ProcessPoolExecutor.map 保序 ⇒ 与串行逐位一致；workers 默认 1 行为不变。"
```

---

### Task 3: `backtest_run.py` 把 `workers` 贯通到选币（select_grids/build_grid_tasks/run_backtest/main）

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`（`select_grids`/`build_grid_tasks`/`run_backtest` 加 `workers` 并透传；`main()` 把 `BT_WORKERS` 传给 `select_grids`）
- Test: `tests/backtest/test_backtest_parallel.py`（加 select_grids 级 parity 测）

**Interfaces:**
- Consumes: `SR.replay_selection(..., workers=1)`（Task 2 产出）。
- Produces: `select_grids(..., workers=1)`、`build_grid_tasks(..., workers=1)`、`run_backtest(..., workers=1)`（新增 `workers`，透传到 `replay_selection`）。

- [ ] **Step 1: 写 select_grids 级并行 parity 测（先失败）**

在 `tests/backtest/test_backtest_parallel.py` 末尾追加：
```python
def test_select_grids_parallel_matches_serial(tmp_path, monkeypatch):
    monkeypatch.setenv('BT_SELECT_CACHE', 'off')          # 隔离缓存（Task 6 后仍只测并行）
    from gridtrade.backtest.backtest_run import select_grids
    from tests.backtest.test_backtest_run import _strategy
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-09 00:00:00'), pd.Timestamp('2024-01-12 00:00:00')
    g1 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h', workers=1)
    g3 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h', workers=3)
    key = lambda gs: [(str(rt), int(off), row['symbol'], round(float(row['close']), 8))
                      for rt, off, row in gs]
    assert len(g1) > 0
    assert key(g1) == key(g3)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_parallel.py::test_select_grids_parallel_matches_serial -v`
Expected: FAIL —— `select_grids() got an unexpected keyword argument 'workers'`。

- [ ] **Step 3: `select_grids` 加 `workers` 并透传**

将 `gridtrade/backtest/backtest_run.py` 的 `select_grids`（第 106-116 行）替换为：
```python
def select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                 *, timeframe='1h', min_quote_volume=0.0, blacklist=(), workers=1, log=print):
    """只跑选币回放（1h + PIT 地板 + 黑名单），返回 [(rt, offset, row)]。offline。"""
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, min_quote_volume=min_quote_volume,
                        blacklist=blacklist, workers=workers, log=log)
    log('[BT] picks=%d' % len(grids))
    return grids
```

- [ ] **Step 4: `build_grid_tasks` 加 `workers` 并透传**

将 `build_grid_tasks`（第 156-164 行）替换为：
```python
def build_grid_tasks(cache, universe, window_start, window_end, strategy_config, factors,
                     *, timeframe='1h', sim_timeframe=None, min_quote_volume=0.0,
                     blacklist=(), workers=1, log=print):
    """选币 + 组装（offline 便捷组合，run_backtest/测试用）。两段式预热见 main()。"""
    grids = select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                         timeframe=timeframe, min_quote_volume=min_quote_volume,
                         blacklist=blacklist, workers=workers, log=log)
    return assemble_grid_tasks(cache, grids, strategy_config,
                               sim_timeframe=sim_timeframe, timeframe=timeframe, log=log)
```

- [ ] **Step 5: `run_backtest` 把 `workers` 也给 `build_grid_tasks`**

将 `run_backtest`（第 193-196 行）中 `build_grid_tasks(...)` 调用替换为（加 `workers=workers`；`simulate_tasks` 调用已带 `workers`，不动）：
```python
    tasks = build_grid_tasks(cache, universe, window_start, window_end, strategy_config,
                             factors, timeframe=timeframe,
                             sim_timeframe=sim_timeframe, min_quote_volume=min_quote_volume,
                             blacklist=blacklist, workers=workers, log=log)
```

- [ ] **Step 6: `main()` 提前取 `BT_WORKERS` 并传给 `select_grids`**

在 `gridtrade/backtest/backtest_run.py` 的 `main()` 中：
1. 在 `cache = ParquetCache(root)`（第 303 行）之后、`print('[BT] window ...')` 之前，插入一行：
```python
    workers = int(os.environ.get('BT_WORKERS', '1'))
```
2. 将 `select_grids(...)` 调用（第 317-319 行）改为带 `workers=workers`：
```python
    grids = select_grids(cache, universe, win_start, win_end, HL_STRATEGY, HL_FACTORS,
                         timeframe='1h', min_quote_volume=BT_MIN_QUOTE_VOLUME_24H,
                         blacklist=BT_BLACKLIST, workers=workers)
```
3. 删除原第 328 行重复的 `workers = int(os.environ.get('BT_WORKERS', '1'))`（已提前定义）。`simulate_tasks(..., workers=workers)` 保持不变。

- [ ] **Step 7: 跑 select_grids parity + 现有并行测 + import smoke**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_backtest_parallel.py tests/backtest/test_backtest_run.py -v`
Expected: 全 PASS（新 select_grids parity 绿；旧 `test_parallel_matches_serial` 仍绿 = run_backtest 并行逐位一致；端到端/差分门测不变）。

Run: `TZ=Asia/Shanghai .venv/bin/python -c "import gridtrade.backtest.backtest_run"`
Expected: 无报错（main 改动无语法/引用问题）。

- [ ] **Step 8: 提交**

```bash
git add gridtrade/backtest/backtest_run.py tests/backtest/test_backtest_parallel.py
git commit -m "feat(backtest): workers 贯通选币（select_grids/build_grid_tasks/run_backtest/main）

选币与仿真复用同一 BT_WORKERS（分时段跑、无争用）；main 提前取 workers 传给 select_grids；默认 1 向后兼容。"
```

---

### Task 4: `cache.py` 加 `list_days` + `read_all_days` 复用（DRY）

**Files:**
- Modify: `gridtrade/backtest/cache.py`（加 `list_days`；`read_all_days` 改用它）
- Test: `tests/backtest/test_cache.py`（加 `list_days` 测）

**Interfaces:**
- Produces: `ParquetCache.list_days(namespace, symbol) -> list[str]`（排序后的 `'YYYY-MM-DD'`，目录不存在→`[]`，不读 parquet 内容）。`read_all_days` 行为不变（内部改用 `list_days`）。

- [ ] **Step 1: 写 `list_days` 测（先失败）**

在 `tests/backtest/test_cache.py` 末尾追加：
```python
def test_list_days(tmp_path):
    import pandas as pd
    from gridtrade.backtest.cache import ParquetCache
    c = ParquetCache(str(tmp_path))
    assert c.list_days('1h', 'AAA/USDT:USDT') == []                      # 无目录 → 空
    for day in ['2024-01-03', '2024-01-01', '2024-01-02']:
        c.write('1h', 'AAA/USDT:USDT', day, pd.DataFrame({'a': [1]}))
    assert c.list_days('1h', 'AAA/USDT:USDT') == \
        ['2024-01-01', '2024-01-02', '2024-01-03']                       # 去 .parquet + 排序
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_cache.py::test_list_days -v`
Expected: FAIL —— `'ParquetCache' object has no attribute 'list_days'`。

- [ ] **Step 3: 加 `list_days` + `read_all_days` 复用**

在 `gridtrade/backtest/cache.py` 的 `write_empty` 之后、`read_all_days` 之前，加：
```python
    def list_days(self, namespace, symbol):
        """廉价列举某 symbol 在该 namespace 下已缓存的天（不读 parquet 内容）。
        返回排序后的 'YYYY-MM-DD' 列表；目录不存在则空列表。"""
        d = self._dir(namespace, symbol)
        if not os.path.isdir(d):
            return []
        return sorted(fn[:-len('.parquet')] for fn in os.listdir(d) if fn.endswith('.parquet'))
```

将 `read_all_days`（第 60-79 行）替换为（复用 `list_days`，行为不变：仍排序、仍跳 0 字节空哨兵、仍合并）：
```python
    def read_all_days(self, namespace, symbol):
        """读取某 symbol 在该 namespace 下所有已缓存天的数据，合并返回（按天排序）。"""
        frames = []
        for day in self.list_days(namespace, symbol):
            p = self._path(namespace, symbol, day)
            if os.path.getsize(p) == 0:
                continue
            try:
                frames.append(pd.read_parquet(p))
            except BaseException:
                continue
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)
```

- [ ] **Step 4: 跑 cache 全部测（含 read_all_days 回归）**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_cache.py -v`
Expected: 全 PASS（新 `list_days` 绿；现有 `read_all_days` 相关测仍绿 = 重构行为等价）。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/cache.py tests/backtest/test_cache.py
git commit -m "feat(cache): ParquetCache.list_days 廉价列举缓存天；read_all_days 复用之（DRY）"
```

---

### Task 5: 新建 `select_cache.py`（key=选币参数+数据指纹，落盘 + 双校验）

**Files:**
- Create: `gridtrade/backtest/select_cache.py`
- Test: `tests/backtest/test_select_cache.py`（新）

**Interfaces:**
- Consumes: `ParquetCache.root`、`ParquetCache.list_days(timeframe, symbol)`（Task 4 产出）。
- Produces:
  - `enabled() -> bool`（`BT_SELECT_CACHE` != `off`）
  - `compute_key(cache, universe, window_start, window_end, timeframe, min_quote_volume, blacklist, strategy_config, factors) -> (str, dict)`
  - `load(cache, key, params) -> grids | None`
  - `save(cache, key, params, grids) -> None`
  - `CACHE_VERSION = 1`

- [ ] **Step 1: 写缓存单测（先失败）**

Create `tests/backtest/test_select_cache.py`:
```python
import os

import pandas as pd

from tests.backtest.test_selection_replay import _seed_cache, _bars, STRAT, FACTORS


def test_compute_key_deterministic_and_sensitive(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    k1, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    k2, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert k1 == k2                                              # 确定性
    k3, _ = SC.compute_key(cache, syms, ws, we, '1h', 1e6, (), STRAT, FACTORS)
    assert k3 != k1                                             # min_quote_volume 改 → 换 key
    k4, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (),
                           dict(STRAT, choose_symbols=2), FACTORS)
    assert k4 != k1                                             # choose_symbols 改 → 换 key


def test_save_load_roundtrip(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert SC.load(cache, key, params) is None                 # 未写 → MISS
    grids = [(pd.Timestamp('2024-01-10'), 0, pd.Series({'symbol': 'AAA/USDT:USDT', 'close': 1.0}))]
    SC.save(cache, key, params, grids)
    got = SC.load(cache, key, params)
    assert got is not None and len(got) == 1
    assert got[0][2]['symbol'] == 'AAA/USDT:USDT' and got[0][1] == 0


def test_load_rejects_param_mismatch(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    SC.save(cache, key, params, [(ws, 0, pd.Series({'symbol': 'X', 'close': 1.0}))])
    tampered = dict(params, choose_symbols=999)                # params 不一致 → 拒（防碰撞）
    assert SC.load(cache, key, tampered) is None


def test_fingerprint_changes_with_new_day(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    k1, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    cache.write('1h', 'AAA/USDT:USDT', '2024-02-01',
                _bars('AAA/USDT:USDT', n=5, start='2024-02-01'))   # 新增一天 → 指纹变
    k2, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert k2 != k1


def test_enabled_env(monkeypatch):
    from gridtrade.backtest import select_cache as SC
    monkeypatch.delenv('BT_SELECT_CACHE', raising=False)
    assert SC.enabled() is True
    monkeypatch.setenv('BT_SELECT_CACHE', 'off')
    assert SC.enabled() is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_select_cache.py -v`
Expected: FAIL —— `No module named 'gridtrade.backtest.select_cache'`。

- [ ] **Step 3: 实现 `select_cache.py`**

Create `gridtrade/backtest/select_cache.py`:
```python
"""选币结果磁盘缓存：key = 选币参数 + 每币缓存天范围数据指纹；命中即秒回，跳过整段选币计算。
仅回测离线工具用。version+params 双校验防 sha256 碰撞；BT_SELECT_CACHE=off 旁路。
数据指纹用 ParquetCache.list_days（廉价 listdir），故重新预热改变缓存天时会自动换 key、不返回过期结果；
「就地改写某天旧文件内容」这类指纹盖不住的极少数情况靠 CACHE_VERSION bump / BT_SELECT_CACHE=off 兜底。"""
import hashlib
import json
import os
import pickle
import tempfile

CACHE_VERSION = 1
_NAMESPACE = '_select_cache'


def enabled():
    return os.environ.get('BT_SELECT_CACHE', 'on').lower() != 'off'


def _fingerprint(cache, universe, timeframe):
    """每个 symbol 的缓存天范围 [最早日, 最晚日, 天数]；无缓存→None。"""
    fp = {}
    for s in sorted(universe):
        days = cache.list_days(timeframe, s)
        fp[s] = [days[0], days[-1], len(days)] if days else None
    return fp


def compute_key(cache, universe, window_start, window_end, timeframe,
                min_quote_volume, blacklist, strategy_config, factors):
    """返回 (key_hex16, params_dict)。params 含数据指纹，重新预热改变缓存天时自动换 key。"""
    params = {
        'version': CACHE_VERSION,
        'window_start': str(window_start),
        'window_end': str(window_end),
        'timeframe': timeframe,
        'universe': sorted(universe),
        'blacklist': sorted(blacklist),
        'min_quote_volume': float(min_quote_volume),
        'period': strategy_config['period'],
        'weight_list': list(strategy_config['weight_list']),
        'choose_symbols': strategy_config['choose_symbols'],
        'max_candle_num': strategy_config['max_candle_num'],
        'factors': {k: bool(v) for k, v in factors.items()},
        'fingerprint': _fingerprint(cache, universe, timeframe),
    }
    blob = json.dumps(params, sort_keys=True, default=str)
    key = hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]
    return key, params


def _dir(cache):
    return os.path.join(cache.root, _NAMESPACE)


def _path(cache, key):
    return os.path.join(_dir(cache), '%s.pkl' % key)


def load(cache, key, params):
    """命中且 version+params 完全一致 → 返回 grids；否则 None。"""
    p = _path(cache, key)
    if not (os.path.exists(p) and os.path.getsize(p) > 0):
        return None
    try:
        with open(p, 'rb') as f:
            obj = pickle.load(f)
    except BaseException:
        return None
    if obj.get('version') != CACHE_VERSION or obj.get('params') != params:
        return None                       # 防 sha256 碰撞 / 版本漂移
    return obj.get('grids')


def save(cache, key, params, grids):
    """原子写 pkl（临时文件 + os.replace）。"""
    d = _dir(cache)
    os.makedirs(d, exist_ok=True)
    p = _path(cache, key)
    fd, tmp = tempfile.mkstemp(dir=d, suffix='.tmp')
    os.close(fd)
    try:
        with open(tmp, 'wb') as f:
            pickle.dump({'version': CACHE_VERSION, 'params': params, 'grids': grids}, f)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
```

- [ ] **Step 4: 跑缓存单测**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_select_cache.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add gridtrade/backtest/select_cache.py tests/backtest/test_select_cache.py
git commit -m "feat(backtest): select_cache 选币结果磁盘缓存（key=选币参数+数据指纹，pickle+双校验）"
```

---

### Task 6: 把 `select_cache` 接进 `select_grids`（HIT/MISS 收口）

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`（`select_grids` 包一层缓存）
- Test: `tests/backtest/test_select_cache.py`（加集成测：命中跳算 / off 不写 / 并行+缓存）

**Interfaces:**
- Consumes: `select_cache.enabled/compute_key/load/save`（Task 5）、`SR.replay_selection(..., workers=)`（Task 2）。
- Produces: `select_grids(...)` 行为不变的返回值，但同参数+同数据第二次调用走磁盘缓存（秒回）。

- [ ] **Step 1: 写集成测（先失败）**

在 `tests/backtest/test_select_cache.py` 末尾追加：
```python
def test_select_grids_cache_hit_skips_recompute(tmp_path, monkeypatch):
    import gridtrade.backtest.selection_replay as SR
    from gridtrade.backtest.backtest_run import select_grids
    from tests.backtest.test_backtest_run import _strategy
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-09'), pd.Timestamp('2024-01-12')
    g1 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h')   # MISS 写
    assert len(g1) > 0

    def _boom(*a, **k):
        raise AssertionError('cache HIT 不应再调 replay_selection')
    monkeypatch.setattr(SR, 'replay_selection', _boom)
    g2 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h')   # HIT 读
    key = lambda gs: [(str(rt), int(off), row['symbol']) for rt, off, row in gs]
    assert key(g1) == key(g2)


def test_select_grids_cache_off_never_writes(tmp_path, monkeypatch):
    from gridtrade.backtest.backtest_run import select_grids
    from tests.backtest.test_backtest_run import _strategy
    monkeypatch.setenv('BT_SELECT_CACHE', 'off')
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h')
    assert not os.path.isdir(os.path.join(str(tmp_path), '_select_cache'))          # off → 不落盘


def test_select_grids_parallel_then_cache_hit(tmp_path):
    from gridtrade.backtest.backtest_run import select_grids
    from tests.backtest.test_backtest_run import _strategy
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-09'), pd.Timestamp('2024-01-12')
    g3 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h', workers=3)  # MISS 并行写
    g1 = select_grids(cache, syms, ws, we, _strategy(), FACTORS, timeframe='1h', workers=1)  # HIT 读
    key = lambda gs: [(str(rt), int(off), row['symbol'], round(float(row['close']), 8))
                      for rt, off, row in gs]
    assert len(g3) > 0 and key(g1) == key(g3)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_select_cache.py::test_select_grids_cache_hit_skips_recompute -v`
Expected: FAIL —— 第二次调用仍走 `replay_selection`（被 `_boom` 触发 AssertionError），因为 `select_grids` 尚未接缓存。

- [ ] **Step 3: `select_grids` 包缓存**

将 `gridtrade/backtest/backtest_run.py` 的 `select_grids`（Task 3 改后版本）替换为：
```python
def select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                 *, timeframe='1h', min_quote_volume=0.0, blacklist=(), workers=1, log=print):
    """只跑选币回放（1h + PIT 地板 + 黑名单），返回 [(rt, offset, row)]。offline。
    结果按选币参数 + 每币缓存天范围数据指纹磁盘缓存（BT_SELECT_CACHE=off 旁路）。"""
    from gridtrade.backtest import select_cache as SC
    use_cache = SC.enabled()
    key = params = None
    if use_cache:
        key, params = SC.compute_key(cache, universe, window_start, window_end, timeframe,
                                     min_quote_volume, blacklist, strategy_config, factors)
        hit = SC.load(cache, key, params)
        if hit is not None:
            log('[BT] select cache HIT %s (picks=%d)' % (key, len(hit)))
            return hit
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, min_quote_volume=min_quote_volume,
                        blacklist=blacklist, workers=workers, log=log)
    log('[BT] picks=%d' % len(grids))
    if use_cache:
        SC.save(cache, key, params, grids)
        log('[BT] select cache MISS %s (saved)' % key)
    return grids
```

- [ ] **Step 4: 跑集成测 + 选币/回测相关全套**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest tests/backtest/test_select_cache.py tests/backtest/test_backtest_parallel.py tests/backtest/test_backtest_run.py -v`
Expected: 全 PASS。要点：`test_select_grids_cache_hit_skips_recompute`（HIT 不重算）、`test_select_grids_cache_off_never_writes`（off 不落盘）、`test_select_grids_parallel_then_cache_hit`（并行写→HIT 读一致）；旧 `test_parallel_matches_serial`/`test_run_backtest_floor_and_blacklist_gate_selection`（不同参数→不同 key→各自 MISS，结果不变）仍绿。

- [ ] **Step 5: 全套回归**

Run: `TZ=Asia/Shanghai .venv/bin/python -m pytest`
Expected: 全套绿。

- [ ] **Step 6: 提交**

```bash
git add gridtrade/backtest/backtest_run.py tests/backtest/test_select_cache.py
git commit -m "feat(backtest): select_grids 接磁盘缓存（HIT 秒回跳过选币；MISS 算完落盘；off 旁路）"
```

---

## Self-Review

**1. Spec coverage：**
- 支柱 A（两 warning）→ Task 1（base→offset + 删 debug 块 + 清 pyproject ignore + 等价测 + 无告警测 + 金标）。✓
- 支柱 B（并行选币）→ Task 2（`_select_over_run_times`/`_replay_chunk`/`_split_contiguous`/`replay_selection workers`）+ Task 3（贯通 select_grids/build_grid_tasks/run_backtest/main）。✓
- 支柱 C（磁盘缓存）→ Task 4（`list_days`）+ Task 5（`select_cache` key/指纹/load/save）+ Task 6（接进 select_grids）。✓
- 支柱 D（接线）→ Task 3 + Task 6。✓
- 测试清单（金标 / base↔offset 等价 / 并行 parity / 缓存命中/key 敏感/指纹失效/并行+缓存/off）→ 分散到各 Task 且齐全。✓

**2. Placeholder scan：** 无 TBD/TODO；每个改动步骤都给出完整代码与确切命令/预期。✓

**3. Type consistency：**
- `replay_selection(..., workers=1)` 在 Task 2 定义，Task 3 `select_grids` 以 `workers=workers` 调用 —— 一致。✓
- `_replay_chunk` payload 11 元组顺序（cache, symbols, chunk, timeframe, period, weight_list, factors, choose_symbols, max_candle_num, min_quote_volume, blacklist）与 `replay_selection` 构造顺序一致。✓
- `select_cache.compute_key(cache, universe, window_start, window_end, timeframe, min_quote_volume, blacklist, strategy_config, factors)` 的实参顺序在 Task 5 测试与 Task 6 `select_grids` 调用一致。✓
- `ParquetCache.list_days(namespace, symbol)`（Task 4）被 `select_cache._fingerprint`（Task 5）以 `cache.list_days(timeframe, s)` 调用 —— 签名一致。✓
- `select_grids` 三个版本（Task 3 加 workers → Task 6 包缓存）演进连贯，最终版含 workers + 缓存。✓
