# 回测票池与 prod 同步（PIT 全市场动态票池）设计

> 日期：2026-07-04　状态：已实现
> 目标：让回测候选票池按 prod `resolve_live_universe` **同口径**、**逐 run_time PIT 动态**生成——全市场 swap 永续 −黑名单 −`$1M` 24h 成交额地板；成交额从缓存 1h K 线重建。**不碰选币数学（因子/55%/排名/offset）、不碰网格参数**。只动回测的候选池构建与预热编排。

## 1. 背景

回测候选池写死 `backtest_run.py:HL_UNIVERSE`（8 币，含 DOGE meme）；prod 已改全市场动态（swap 去重 −黑名单 −`$1M` 地板 → ~60 币/小时变）。选币数学共享（`selection_replay` 复用 `core.selection`），但**候选池天差地别** → 回测调参/收益不代表 prod。这个 gap 是老问题（旧 prod 白名单 26 vs 回测 8），本次移植把它拉更大（prod ~60 全市场）。

## 2. 已确认决策

| 决策点 | 选择 |
|---|---|
| 候选集 | **全市场**：`adapter.list_instruments()`（现已 swap+去重）−黑名单；替代写死 `HL_UNIVERSE` |
| 历史 24h 成交额源 | **从缓存 1h K 线重建**：`Σ(前置 24 根 1h bar.quote_volume, <run_time)`（HL 无历史 dayNtlVlm 端点，唯一可行；point-in-time、无未来函数） |
| 地板阈值 | `MIN_QUOTE_VOLUME_24H`，默认 **$1M 对齐 prod**（可配） |
| 预热结构 | **两段式**：全市场先预热 1h → 跑带地板的选币 → 只给选中币预热 1m/funding → 再 sim |

## 3. 数据流

```
候选 = adapter.list_instruments()[state==live, swap+去重] − 黑名单        # ①
  → 预热全市场 1h K线                                                     # ③ phase1
  → 逐 run_time (selection_replay 内):                                    # ②
       每币 vol24 = sub[candle_begin_time<run_time].tail(24).quote_volume.sum()
       过滤: vol24 >= MIN_QUOTE_VOLUME_24H  且  币 ∉ 黑名单
       → 幸存者喂 select_grid_coin(55%相对 + 因子排名)                    # 现有不动
  → 选中币集 S → 预热 S 的 1m/funding → 拼 sim → simulate                 # ③ phase2
```

## 4. 组件

### ① 候选票池全市场化（`backtest_run.py`）
- 删写死 `HL_UNIVERSE`；票池由 `adapter.list_instruments()`（`prewarm_all` 里已有 `_RetryHL` 适配器）取 `state=='live'` 的 swap 去重符号 −黑名单。
- 加回测 config：`BT_MIN_QUOTE_VOLUME_24H`（默认 `1_000_000.0`，对齐 prod）、`BT_BLACKLIST`（默认空 tuple，对齐 prod）。可经 env/CLI 覆写做敏感性扫描。
- 注意：`list_instruments` 需要一个 adapter；`prewarm_all` 已构造 `_RetryHL`。全市场解析放在预热编排处（main/prewarm_all），把解析出的 universe 传给后续。

### ② PIT 地板 + 黑名单过滤（`selection_replay.replay_selection`）
- 在逐 run_time 循环、组装 `symbol_candle_data` 时，对每个币的已截断 `sub`（`candle_begin_time < run_time`）先算 `vol24 = sub.tail(24)['quote_volume'].sum()`；`vol24 < min_quote_volume` 或币 ∈ 黑名单 → **不纳入** `symbol_candle_data`（等价于该 run_time 该币不在候选池）。
- 已有 `len(sub) < 24: continue` 保证 ≥24 根，故 `tail(24)` 安全。
- `replay_selection` 新增参数 `min_quote_volume=0.0, blacklist=()`（默认 0/空 → 行为与现状一致、向后兼容；旧测试不改）。
- 关键：地板是 **PIT 截面前置过滤**，在 `select_grid_coin` 的 55% 相对过滤**之前**——两者叠加即"绝对地板 + 相对前 55%"，与 prod 管线同序。

### ③ 两段式预热（`backtest_run.py` 编排重构）
现状：`prewarm_all(cache, universe, ...)` 一次性给整池预热 1h+1m+funding；`build_grid_tasks` 先 `load_full_series(sim_tf)` 载整池 sim 再选币。全市场 ~176 币下给整池拉 1m 会爆炸。改为：
- **拆 `build_grid_tasks`**：
  - `select_grids(cache, universe, run_times, strategy_config, factors, min_quote_volume, blacklist, ...) -> grids`（只跑 `replay_selection`，1h + 地板，offline）。
  - `assemble_grid_tasks(cache, grids, universe_selected, strategy_config, sim_timeframe, ...) -> data_tasks`（原 build_grid_tasks 的 sim 组装段：`load_full_series(选中币, sim_tf)` + `holding_bars` + funding 切片）。
- **`main()` 编排**（网络/离线分离不破）：
  1. 解析全市场 universe（`list_instruments` −黑名单）。
  2. `prewarm_ohlcv(1h, 全市场)`（选币只需 1h；funding 与 1m 一样放 phase2、仅选中币）。
  3. `select_grids(...)`（offline，1h）→ 选中币集 `S = {row['symbol']}`。
  4. `prewarm_ohlcv(sim_tf, S)` + `prewarm_funding(S)`（网络，仅选中币）。
  5. `assemble_grid_tasks(cache, grids, S, ...)` → `simulate_tasks(...)`。
- `run_backtest`（离线纯核心，测试用）相应改为吃"已解析 universe + min_quote_volume + blacklist"，内部走 select→assemble（**假设 cache 已预热**，不触网——测试仍可全离线喂 cache）。

## 5. 测试

- **② PIT 地板（核心）**：造 3-4 币 1h 序列，令某币的**前置 24h quote_volume 在窗口内跨越门槛**；断言：门槛下的 run_time 该币被剔（不进 symbol_candle_data / 不被选），门槛上的 run_time 保留；**无未来函数**（只用 `<run_time` bar）；黑名单币恒剔。`min_quote_volume=0` 时行为与现状一致。
- **① 候选**：mock adapter.list_instruments（swap+非 swap+重复）→ 断言候选 = 去重 swap −黑名单。
- **③ 两段式**：mock DataSource 记录 `prewarm_ohlcv` 调用；断言 1h 用全市场、1m/funding 只用选中币集。`select_grids`/`assemble_grid_tasks` 拆分后各自可测。
- **端到端**：小型 mock（3-4 币、构造量能跨门槛）离线 `run_backtest` → 断言选中集符合 PIT 地板、pnl 列非 NaN（沿用现有 e2e 结构断言）。
- 全套 `pytest` 绿（回测测试 + 不回归其它）。

## 6. 忠实度边界（诚实标注）

- **近似**：缓存 K 线 `quote_volume`（≈量×midprice）之和 ≈ live `dayNtlVlm`，量级一致、非 byte 精确 → `$1M` 阈值是近似（可按需微调）。
- **存活者偏差**：只见当前在市币（与 prod `list_instruments` 同口径，可接受）。历史退市但曾高量的币缺席。
- **无未来函数**：地板只用 `< run_time` 的 bar，与选币 PIT 纪律一致。
- 仍非"每小时完全动态复刻"上限，但已是离线能做到的最贴 prod。

## 7. 不在本次范围

- 不改 `core.selection`（因子/55%/排名/offset）、`grid_params`、止损。
- 不追求 byte 精确 `dayNtlVlm`。
- 不实现历史退市币回补（幸存者偏差保留）。
- 不改 prod / live 路径（本改动仅回测）。
