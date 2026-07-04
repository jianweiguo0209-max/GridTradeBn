# 回测选币性能优化 + selection.py warning 修复 — 设计

> 状态：设计已与用户确认，待写实现计划。
> 日期：2026-07-04

## 目标（一句话）

把回测「选币回放（selection）」从单核串行改为**多进程并行**，并为选币结果加**磁盘缓存**（重复跑同一窗口秒回），同时修掉 `gridtrade/core/selection.py` 的两个 warning —— 全程与串行**逐位一致**、金标 parity 保持绿。

## 背景与瓶颈

- 回测流水线：`resolve_universe` →（1h 预热）→ **`select_grids`/`replay_selection`（选币，单核串行，瓶颈）**→ 仅选中币预热 1m/funding → `assemble_grid_tasks` → `simulate_tasks`（**已并行**，`BT_WORKERS`+`ProcessPoolExecutor`）。
- 实测选币 ~0.72 run_time/s；4 个月窗口 ~2929 个 run_time → 单核 ~40–70 分钟，是总耗时的主导项。各 run_time 相互独立 → 天然可并行。
- 两个 warning 都在 `gridtrade/core/selection.py`（**golden-locked，且与 live/prod 共享同一份选币纯函数**），每个 run_time 复现、污染 stderr：
  - `[selection.py:32]` `FutureWarning: 'base' in .resample() is deprecated`，来自 `trans_period_for_grid` 的 `data.resample(rule=period, base=offset)`。
  - `[selection.py:129-135]` `SettingWithCopyWarning`，来自 `select_grid_coin` 里遗留的 debug-print 块（`pdata[...] = ...` + `print("当前周期的全集选币排序")`）。该块还在 prod 日志刷屏、每 run_time 有 DataFrame 格式化开销。

> 关键事实：两个 warning 走 `warnings`→**stderr**，`replay_selection` 里的 `redirect_stdout(devnull)` 只挡 stdout、**挡不住 warning** —— 这正是为何预热日志里照样能看到它们。所以真正的修复是「删 debug 块 + 改 base→offset」，`redirect_stdout` **不动**（它还要继续抑制 `proceed_calc_symbol_factor` 里合法但吵的 `no data`/`no enough data`/`[警告]` 三处 print）。

## 范围

**做**：① 修两个 warning；② 选币多进程并行（复用 `BT_WORKERS`）；③ 选币结果磁盘缓存（params + 数据指纹）。

**不做**（明确非目标）：
- 不并行网络预热（1h/funding 逐币 ccxt 拉取）——冷跑 I/O-bound、暖跑基本 cache-skip、并行有 HL 限频风险且难确定性测试。
- 不动 `simulate_tasks`（已并行）。
- 不改网格参数 / 因子算法 / 选币逻辑本身（`core.selection` 只做两处 warning 的最小改动，且金标兜底）。
- 不碰 prod 部署 / production 分支。

## 全局约束（每个任务都隐含）

- Python 3.9 / pandas 1.3.5 / numpy 1.22.4 / pyarrow（依赖冻结，不新增第三方库；并行用标准库 `concurrent.futures`、`multiprocessing`）。
- **金标 parity 不可破**：`tests/core/test_selection_parity.py`、`tests/core/test_factors_parity.py` 改动后逐位绿（它们锁的是**返回值**、非 stdout）。
- **并行/缓存结果与串行逐位一致**：`select_grids(workers=1)` 与 `workers=N`、缓存 MISS 与 HIT，返回的 `grids` 列表逐条完全相等（`(rt, offset, symbol, row 全字段)`）。
- 新增参数一律**默认关**（`workers=1`、缓存默认开但可 `off`），保向后兼容；旧调用签名不破。
- 测试命令：`TZ=Asia/Shanghai .venv/bin/python -m pytest`。

## 文件与职责

| 文件 | 改动 |
|---|---|
| `gridtrade/core/selection.py` | ① `:32` `base=offset`→`offset=pd.Timedelta(hours=offset)`；② 删 `:129-135` debug 块。**仅此两处**。 |
| `gridtrade/backtest/selection_replay.py` | 抽 `_select_over_run_times`（循环体纯函数）；加顶层 worker `_replay_chunk`；`replay_selection` 加 `workers=1`。 |
| `gridtrade/backtest/select_cache.py`（新） | 选币结果磁盘缓存：key 计算（params + 数据指纹）、读/写、version 校验、env 旁路。 |
| `gridtrade/backtest/cache.py` | 加 `list_days(namespace, symbol)`（廉价 listdir、不读 parquet）；`read_all_days` 复用之（DRY，行为不变）。 |
| `gridtrade/backtest/backtest_run.py` | `select_grids`/`build_grid_tasks`/`run_backtest` 加 `workers=1` 透传；`select_grids` 包一层磁盘缓存；`main()` 把 `BT_WORKERS` 传给 `select_grids`。 |
| `tests/core/test_selection_parity.py`（或新 `test_resample_offset_equiv.py`） | base↔offset 全相位（0..11）等价小测。 |
| `tests/backtest/test_selection_replay.py` / `test_backtest_parallel.py` | 并行-parity 测（workers=1 vs 3 逐条相等）。 |
| `tests/backtest/test_select_cache.py`（新） | 缓存命中往返 / key 敏感性 / 数据指纹失效 / 并行+缓存组合。 |

---

## 支柱 A：warning 修复（`core/selection.py`，最小改动）

**A1 — `base`→`offset`（`:32`）**

```python
# 旧
period_df = data.resample(rule=period, base=offset).agg(agg_dict)
# 新
period_df = data.resample(rule=period, offset=pd.Timedelta(hours=offset)).agg(agg_dict)
```

`base=N`（子频率原点，单位=频率的子小时，此处 0..11）与 `offset=Timedelta(hours=N)` 语义等价。`offset` 恒为 UTC 小时 % 12（`compute_offset`）。

**验证**：新增 base↔offset 全相位等价测（同一份 data，`k∈0..11`，两写法 `assert_frame_equal`），补金标只覆盖 offset=0 的盲区；金标 parity 逐位绿。

**A2 — 删 debug 块（`:129-135`）**

删除：
```python
# 测试用：打印当前周期的全集排序
pdata = data[(data['time'] + pd.to_timedelta('12H')) >= run_time]
pdata.sort_values(by='rank', inplace=True)
pdata["time"] = pdata["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
print("当前周期的全集选币排序")
print(pdata.head(10))
# exit()
```

该块只读 `data`、造一个丢弃用的 `pdata`、打印，**不修改 `data`、不影响返回** → 金标绿。删除同时消除 `SettingWithCopyWarning` + prod 日志刷屏 + 每 run_time 的格式化开销。

**保留**：`proceed_calc_symbol_factor` 的 `no data` / `no enough data` / `[警告]…` 三处 print 不动（合法诊断，仍由 `replay_selection` 的 `redirect_stdout` 抑制）。

---

## 支柱 B：选币多进程并行（`selection_replay.py`）

**B1 — 抽循环体为纯函数**

```python
def _select_over_run_times(series, run_times, period, weight_list, factors,
                           choose_symbols, max_candle_num, min_quote_volume, blacklist):
    """现 replay_selection for-loop 的循环体：逐 run_time 选币，返回 [(rt, offset, row)]。
    内部 redirect_stdout 抑制 core 诊断 print。串行/并行共用。"""
    out = []
    devnull = open(os.devnull, 'w')
    try:
        for run_time in run_times:
            run_time = pd.Timestamp(run_time)
            offset = compute_offset(run_time, period)
            scd = build_pit_candidates(series, run_time, max_candle_num=max_candle_num,
                                       min_quote_volume=min_quote_volume, blacklist=blacklist)
            if not scd:
                continue
            with contextlib.redirect_stdout(devnull):
                all_df = proceed_calc_symbol_factor(scd, run_time, period, offset)
                if all_df is None or all_df.empty:
                    continue
                factor_data = select_grid_coin(all_df, factors, weight_list, choose_symbols, run_time)
            factor_data = factor_data[(factor_data['time'] + pd.to_timedelta(period)) >= run_time]
            for _, row in factor_data.iterrows():
                out.append((run_time, offset, row.copy()))
    finally:
        devnull.close()
    return out
```

**B2 — 顶层可 pickle worker**

```python
def _replay_chunk(payload):
    """进程池 worker：各自从本地缓存加载 series（无大 pickle），选自己那段 run_time。"""
    (cache, symbols, run_times_chunk, period, weight_list, factors,
     choose_symbols, max_candle_num, min_quote_volume, blacklist, timeframe) = payload
    series = load_full_series(cache, symbols, timeframe)
    return _select_over_run_times(series, run_times_chunk, period, weight_list, factors,
                                  choose_symbols, max_candle_num, min_quote_volume, blacklist)
```

`ParquetCache` 只含 `root` 字符串 → 可 pickle，安全传 worker。`row` 为 pandas Series → 可 pickle。

**B3 — `replay_selection(..., workers=1)`**

- `workers<=1`：串行 —— `series=load_full_series(...)` 一次 → `_select_over_run_times(全量)` → 逐条 `on_select(rt,offset,row)`。**与现行为逐位一致**。
- `workers>1`：`run_times` 切成 `W=min(workers, len(run_times))` 段**连续等分**块 → `ProcessPoolExecutor.map(_replay_chunk, payloads)`（**map 保输入序**）→ 按块序 flatten → 逐条 `on_select`。**连续切分 + map 保序 ⇒ 无需重排，输出与串行逐位一致**。
- 返回 `processed = len(run_times)`（现实现每 run_time 恒 `+1`，等价）。

**数据流**
```
run_times(有序) → 切 W 个连续块 → ProcessPoolExecutor.map(_replay_chunk)
   worker: load_full_series + _select_over_run_times(块)
→ 块按序返回(map 保序) → flatten → on_select(rt,offset,row)  # 主进程
```

**并行成本注记**：进程启动 + 每 worker 一次 `load_full_series`（全市场 ~266 币）是固定开销 → 仅 run_time 足够多时净赚；默认 `workers=1`，由 `BT_WORKERS` 显式开。选币任务近似 CPU-flat（暖缓存后各 run_time 候选数大致恒定）→ 连续等分块负载均衡良好。

---

## 支柱 C：选币结果磁盘缓存（新 `select_cache.py` + `select_grids` 收口）

**C1 — 缓存 key**

`key = sha256(canonical_json(下列, sort_keys=True))[:16]`：

- `CACHE_VERSION`（常量；选币逻辑一改就 bump → 全失效）
- `window_start`、`window_end`、`timeframe`
- `sorted(universe)`、`sorted(blacklist)`、`min_quote_volume`
- 选币相关 strategy_config 子集：`period`、`weight_list`、`choose_symbols`、`max_candle_num`
- `factors`（含各因子 ascending 标志，canonical 化）
- **数据指纹**：对 `sorted(universe)` 每个 symbol，`cache.list_days(timeframe, s)` → `(days[0], days[-1], len(days))`（无缓存→`None`）。盖住「重新预热延长历史 / 增删币」导致的过期。

`workers` **不进 key**（不影响结果）。

**C2 — 存储与校验**

- 路径：`data/hl_validate/_select_cache/<key>.pkl`。
- 内容：`pickle({'version': CACHE_VERSION, 'params': <上述 key 明细 dict>, 'grids': grids})`。
- 加载时**校验 `version` 与 `params` 完全一致**（防 sha256 碰撞，几乎零成本），不一致 → 当 MISS 重算。
- 格式选 pickle：`grids` 里是 pandas Series，pickle 精确往返、最省事（本地离线开发缓存，非跨机/跨版本产物）。

**C3 — 流程（在 `select_grids` 内）**

```
算 key → 命中文件 且 未 off 且 version+params 校验通过 → 读盘返回(log "select cache HIT <key>")
否则 → 算(_select_over_run_times / 并行) → 写盘 → 返回(log "select cache MISS <key>")
```

**C4 — 开关与失效**

- `BT_SELECT_CACHE=off` → 整体旁路（**不读不写**，干净重算）。默认开（读+写）。
- 手动清：`rm -rf data/hl_validate/_select_cache`。
- version-bump / `off` 兜底「就地改写某天文件内容」这类指纹盖不住的极少数情况（文档写明）。

**C5 — `cache.py` 加 `list_days`**

```python
def list_days(self, namespace, symbol):
    """廉价列举已缓存天（不读 parquet 内容）。返回排序后的 'YYYY-MM-DD' 列表。"""
    d = self._dir(namespace, symbol)
    if not os.path.isdir(d):
        return []
    return sorted(fn[:-len('.parquet')] for fn in os.listdir(d) if fn.endswith('.parquet'))
```
`read_all_days` 改为遍历 `self.list_days(...)`（DRY，行为不变：仍排序、仍只 `.parquet`、仍跳 0 字节空哨兵）。

---

## 支柱 D：接线（`backtest_run.py`）

- `select_grids(..., workers=1)`：包缓存（C3）；MISS 时把 `workers` 透传 `replay_selection`。
- `build_grid_tasks(..., workers=1)` / `run_backtest(..., workers=1)`：透传（默认 1，向后兼容）。
- `main()`：已有 `workers=int(os.environ.get('BT_WORKERS','1'))`，把它也传给 `select_grids(..., workers=workers)`。**选币与仿真复用同一个 `BT_WORKERS`**（分时段跑、无争用；一个「用几核」旋钮）。

---

## 测试

**金标（回归护栏）**
- `tests/core/test_selection_parity.py`、`test_factors_parity.py` 保持绿（证 A1+A2 无漂移）。

**A — base↔offset 等价**
- 同一份构造 data，`k∈0..11`：`resample(base=k)` vs `resample(offset=Timedelta(hours=k))` → `assert_frame_equal`。

**B — 并行 parity**
- 小 `ParquetCache` fixture（几币 × 若干天 1h，跨多个 run_time 且有选中）：`select_grids(workers=1)` vs `workers=3` → 断言 `grids` 逐条完全相等（`(rt, offset, symbol, row 全字段)`）。
- 关掉缓存（`BT_SELECT_CACHE=off` 或用独立 tmp 根）以隔离本测只验并行。

**C — 磁盘缓存**
- 命中往返：`select_grids`（MISS 写）→ 再调（HIT 读）→ grids 逐条相等，且 HIT 路径**不再调** `replay_selection`（monkeypatch 成命中时调用即抛 → 证走缓存）。
- key 敏感性：改 `choose_symbols` / `min_quote_volume` / `universe` → 不同 key → 重算（非返回旧值）。
- 数据指纹失效：命中后往缓存**新增一天** 1h bar（改变 `list_days` 指纹）→ 变 key → MISS 重算（不返回过期）。
- 并行+缓存组合：`workers=3`（MISS 写）→ `workers=1`（HIT 读）→ grids 逐条相等。
- `BT_SELECT_CACHE=off`：不写文件、每次重算。

**全套**：`TZ=Asia/Shanghai .venv/bin/python -m pytest` 绿。

## 收益

- 选币 CPU-bound、各 run_time 独立 → 近似 ×核数；4 个月 ~40–70min → 个位数分钟（8 核）。
- 磁盘缓存：重复跑同一窗口+参数 → 选币阶段秒回（跳过全部计算）。
- 附带：删 debug 块省格式化开销 + 清 prod 日志噪声；两个 warning 归零。

## 风险与缓解

- **base→offset 语义**：全相位（0..11）等价测 + 金标兜底。
- **并行开销**：小窗口可能反慢 → 默认 `workers=1`，显式开。
- **缓存过期**：数据指纹盖住「历史延长/增删币」；「就地改写旧天内容」靠 `CACHE_VERSION` bump / `BT_SELECT_CACHE=off` 兜底（已文档化）。
- **golden-locked 改动**：`core.selection` 仅两处最小改动，金标 parity + base↔offset 等价测双重护栏。
