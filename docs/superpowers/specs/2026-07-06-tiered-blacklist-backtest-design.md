# 三档半拉黑名单可回测（TierPolicy）设计

日期：2026-07-06 ｜ 状态：已批准（用户）｜ 范围：仅回测，不碰实盘/core 选币本体

## 背景

legacy OKX 实盘有三档半拉黑机制（black_dict：档0 硬禁 25 币 / 档1 名单币并发≤1 /
档2 OTHERS 并发≤2，执行位=选币前剔票池、次优自动上位）。现系统移植状态（见移植
对照报告，2026-07-06）：档0 已移植（BLACKLIST_SYMBOLS，9/25 币 HL 在市已填入）；
档1/档2 被 SymbolLockGate 全局每币≤1 替代（更严）；执行位已由方案 A 对齐
（scheduler 选币入口剔他 tag 持有币，次优回退，commit 待部署）。

**本设计回答**：cap=2 vs 1 值多少收益、档0 该禁谁——用回测量化后再决定是否把
档1/档2 移植回实盘。前置事实：四窗回测显示无锁 vs 每币≤1 砍 53-61% 网格、收益
缩水 56-83%（memory backtest-symbollock-gap）；legacy 真实口径 cap=2 落在中间，
量化其位置是本设计的直接动机。

## 目标

- 档0/档1/档2 在回测中可配置、可扫参；语义与实盘方案 A 对齐（剔锁+次优递补）。
- 评估观测面：每档拒绝数、递补深度直方图、空过轮数随结果输出。
- 现状零扰动：`tiers=None` 时逐位不变；既有 `symbol_lock` 参数原样保留（基线可比，
  另一 session 口径），与 tiers 互斥。

## 非目标

- 不动实盘（评估结论出来后另立方案）；不动 core `select_grid_coin` 本体逻辑
  （只放宽 rank 截断参数）；不做实际关格时刻的精确锁释放（循环依赖：需先仿真才知
  关格时刻；固定 period 近似与既有 symbol_lock 过滤器同口径、偏保守）。

## 设计

### 1. TierPolicy（backtest 模块内 dataclass，结构与 legacy black_dict 同构）

```python
@dataclass(frozen=True)
class TierPolicy:
    tier0: tuple = ()      # 硬禁：并入 blacklist 在票池级剔除（连选币数据都不进）
    tier1: tuple = ()      # 名单币并发上限 1
    tier2_cap: int = 2     # 其余币(OTHERS)并发上限；0 = 不限
```

env（main()）：`BT_TIER0`（缺省沿用 BT_BLACKLIST）、`BT_TIER1_SYMBOLS`（csv）、
`BT_TIER2_CAP`（int，默认 2）、`BT_TIER_CAND_K`（递补深度，默认 5）。

### 2. 选币回放保留 top-K 候选

`select_grids(..., candidates_per_rt=1)` 新参数：内部把传给选币的
`choose_symbols` 放宽为 K（即 `rank <= K` 截断），每个 (run_time, offset) 产出
≤K 行带 `rank` 的候选。K=1 时与现状**逐位一致**（回归保真测试钉死）。K 经
strategy_config 指纹自然进入 select 磁盘缓存 key（不同 K 不串缓存）。

### 3. 分配器 `allocate_with_tiers(ranked_picks, tiers, period) -> (picks, stats)`

纯函数，按 run_time 升序扫描：

- **held 计数**：币 → 当前占用数；占用在 `开仓 rt + period` 时刻释放（**固定
  period 锁窗近似**，恰满边界=释放，与 `filter_tasks_symbol_lock` 同口径）。
- **每 run_time**：候选按 rank 升序，取第一个未触顶的币（tier1 名单上限 1，
  其余上限 tier2_cap；tier2_cap=0 视为无限）→ 即方案 A 的次优递补。K 个全触顶
  → 该轮空过。
- **stats**：`{'rejected_tier1': n, 'rejected_tier2': n, 'fallback_hist':
  {递补深度: 次数}, 'empty_rounds': n}`——评估的观测面，随 log 输出。
- 返回 picks 与现有 tasks 输入同形（(rt, offset, row) 元组，row 为选中候选行），
  后续 `assemble_grid_tasks`/`simulate_tasks` 零改动。

### 4. run_backtest 接线

`run_backtest(..., tiers=None)`：
- `tiers=None`：现状零变化（含 symbol_lock 路径）。
- `tiers=TierPolicy(...)`：`blacklist = blacklist + tiers.tier0`；
  `select_grids(candidates_per_rt=K)`；分配器过滤后再 assemble。
- `tiers` 与 `symbol_lock=True` 同时传 → `ValueError`（两套口径不叠加）。

### 5. 近似注记（docstring 必须写明）

1. **全池排名 + 触顶跳选** vs 实盘方案 A 的"剔锁后再排名"：少 1-2 个币会使
   跨截面 per-factor rank 有微小位移，方向无偏；精确对齐需逐 run_time 动态池
   重排，摧毁选币回放的向量化（groupby(time).rank 一次算全窗），不值。
2. **固定 period 锁窗**：实盘止损早退提前释放锁（四窗实测早退 ~4% 格），
   本近似偏保守；与 symbol_lock 过滤器口径一致，两者结果可直接对比。
3. K 不够深时空过计入 stats；K 可调。

### 6. 测试

- 分配器纯函数：tier1=1/OTHERS=cap 语义、边界释放（恰满=放）、递补次序（rank
  升序第一个可用）、K 耗尽空过、tier0 已在票池级不进候选、tier2_cap=0 不限、
  stats 数字正确。
- top-K 管道：`candidates_per_rt=1` 与现状逐位一致（保真回归）；K>1 时每 rt
  行数 ≤K 且 rank 单调。
- e2e 恒等/包含关系：`tiers(tier2_cap=0)` ≡ 无锁基线；
  `tiers(tier2_cap=1, tier1=∅)` 的选中集 ⊇ `symbol_lock=True` 的选中集
  （递补只增不减），且两者拒绝口径差=递补命中数。
- 缓存：不同 K 不命中同一 select 缓存条目。

### 7. 评估用法（交付后的标准跑法，不属实现范围）

四窗 × {无锁, cap=1 不递补(symbol_lock), cap=1 递补, cap=2 递补}
× {tier0=∅, tier0=已移植 9 币}，输出每格收益/格数/递补统计对照表
→ 决定实盘档1/档2 是否移植及参数。

## 改动面

`gridtrade/backtest/backtest_run.py`（TierPolicy + allocate_with_tiers +
select_grids 参数 + run_backtest 接线 + main env）；
`selection_replay`/`select_cache` 仅在需要透传 K 时小改。
core/实盘/部署零改动。
