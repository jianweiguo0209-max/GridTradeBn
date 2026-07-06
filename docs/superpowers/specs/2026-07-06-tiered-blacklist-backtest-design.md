# 三档半拉黑名单可回测（TierPolicy）设计

日期：2026-07-06 ｜ 状态：已批准（用户，含两轮同源性修订）｜ 范围：回测评估能力 + 共享策略层；不改实盘行为

## 背景

legacy OKX 实盘有三档半拉黑机制（black_dict：档0 硬禁 25 币 / 档1 名单币并发≤1 /
档2 OTHERS 并发≤2，执行位=选币前剔票池、次优自动上位）。现系统移植状态（移植对照
报告 2026-07-06）：档0 已移植（BLACKLIST_SYMBOLS，HL 在市 9/25 币）；档1/档2 被
SymbolLockGate 全局每币≤1 替代（更严）；执行位已由方案 A 对齐（选币入口剔他 tag
持有币、次优回退）。

**本设计回答**：cap=2 vs 1 值多少收益、档0 该禁谁——回测量化后再决定档1/档2 是否
移植实盘。前置事实：四窗无锁 vs 每币≤1 砍 53-61% 格、收益缩水 56-83%；legacy 真实
口径 cap=2 落在中间。

## 同源性要求（用户评审两轮补强，本设计的硬约束）

「只改一处导致回测失真」必须从结构上关死，三层处理：

1. **名单单一事实源**：`gridtrade/config.py` 的 `DEFAULT_TIER_POLICY`（与
   DEFAULT_STRATEGY_CONFIG 同级）。实盘与回测都 import 它作默认；env 两侧只作
   **覆盖**（实盘=运维紧急面，回测=扫参面）。fly.toml/fly.prod.toml 里的
   BLACKLIST_SYMBOLS 长名单**撤回为空**（注释指向 config.py），否则 toml 成第二
   事实源。名单变更从此走代码 commit（review/历史/两侧原子生效）。
2. **判定逻辑单一实现**：新模块 `gridtrade/core/tier_policy.py`（core 层无交易所
   依赖，与 selection.py 同级）承载全部策略判定；实盘 scheduler/control_compute
   的方案 A 剔锁改为经它表达（行为不变：现状 = tier2_cap=1），回测分配器同样只调它。
   加**同源守卫测试**防绕过。
3. **不可共享的仅两条，文档化为已知近似边界**：持仓状态供给（实盘=DB 实时活跃格，
   止损早退即时释放；回测=固定 period 锁窗，偏保守 ~4% 格）与排名池时序（实盘剔后
   排名；回测全池排名+触顶跳选，保选币向量化）。

## 目标

- 三档在回测中可配置、可扫参，语义与实盘方案 A 对齐（剔锁+次优递补）。
- 评估观测面：每档拒绝数、递补深度直方图、空过轮数随结果输出。
- 现状零扰动：`tiers=None` 时回测逐位不变；实盘重表达后行为逐位不变；既有
  `symbol_lock` 参数原样保留（另一 session 基线口径），与 tiers 互斥。

## 非目标

- 不改实盘**行为**（重表达≠改行为；cap 调 2/加 tier1 需回测结论后另批准）。
- 不动 core `select_grid_coin` 本体（只放宽 rank 截断参数）。
- 不做实际关格时刻的精确锁释放（循环依赖；固定 period 近似同既有 symbol_lock
  过滤器口径）。

## 设计

### 1. `gridtrade/core/tier_policy.py`（新，共享策略层）

```python
@dataclass(frozen=True)
class TierPolicy:
    tier0: tuple = ()      # 硬禁：票池级剔除
    tier1: tuple = ()      # 名单币并发上限 1
    tier2_cap: int = 2     # 其余币(OTHERS)并发上限；0 = 不限

def effective_blacklist(blacklist, tiers) -> tuple
    # 档0 合并：tuple(dict.fromkeys(blacklist + tiers.tier0))，保序去重；tiers=None 原样

def cap_for(symbol, tiers) -> Optional[int]
    # tier1 内 → 1；否则 tier2_cap（0 → None=不限）。tier0 不在此判（票池级已剔）。

def pick_first_allowed(ranked_symbols, held_counts, tiers) -> Optional[int]
    # 按序取第一个 held_counts.get(sym,0) < cap 的下标（cap None=不限恒可）；
    # 全触顶 → None（空过）。这是实盘方案A与回测递补共用的唯一判定。
```

### 2. `gridtrade/config.py`：名单单源

```python
DEFAULT_TIER_POLICY = TierPolicy(
    tier0=('BTC/USDC:USDC', 'ETH/USDC:USDC', 'VINE/USDC:USDC', 'NEO/USDC:USDC',
           'PEOPLE/USDC:USDC', 'KNEIRO/USDC:USDC', 'MOODENG/USDC:USDC',
           'FARTCOIN/USDC:USDC', 'CFX/USDC:USDC'),   # legacy 档0（HL 在市 9/25；
           # 未上市 16 币不猜译名：PI/DEGEN/ALCH/MAX/OL/MASK/ACT/SONIC/BR/RDNT/
           # MAGIC/CSPR/LOOKS/MEW/NEIROETH/IP，上市巡检再补）
    tier1=(),
    tier2_cap=1,   # 当前实盘现实（SymbolLockGate 每币≤1）；回测评估后另批准再调
)
```

实盘 `load_deploy_config`：`blacklist` 默认 = DEFAULT_TIER_POLICY.tier0（env
BLACKLIST_SYMBOLS 非空则覆盖）。deploy/fly.toml 与 fly.prod.toml 撤回长名单
（空占位+注释指向 config.py）。

### 3. 实盘重表达（行为不变）

scheduler 方案 A 剔锁与 dashboard control_compute 预览：held_counts 从 DB 活跃格
数出（scheduler 侧保留本 tag 豁免——状态供给侧口径），剔除集 = 票池中
`cap_for(s) 已被 held 占满` 的币，即经 `pick_first_allowed`/cap_for 表达。现配置
（tier2_cap=1）下输出与现实现逐位相同（同源守卫测试钉死）。SymbolLockGate 原样
保留作开仓竞态守卫。

### 4. 回测：top-K 候选 + 分配器

- `select_grids(..., candidates_per_rt=1)`：choose_symbols 放宽为 K（rank<=K），
  每 (run_time, offset) ≤K 行带 rank 候选；K=1 与现状逐位一致；K 进 select
  缓存 key（经 strategy_config 指纹）。
- `allocate_with_tiers(ranked_picks, tiers, period) -> (picks, stats)`（backtest
  模块）：时间循环 + held 记账（固定 period 锁窗，恰满边界=释放），每 run_time 调
  共享 `pick_first_allowed`；stats={rejected_tier1, rejected_tier2,
  fallback_hist, empty_rounds}。picks 与现有 tasks 同形，下游
  assemble/simulate 零改动。
- `run_backtest(..., tiers=None)`：None=现状；TierPolicy 时
  blacklist=effective_blacklist(blacklist, tiers)、K 候选、分配器过滤；与
  `symbol_lock=True` 同传 → ValueError。
- env（main()）：`BT_TIER0`/`BT_TIER1_SYMBOLS`/`BT_TIER2_CAP`/`BT_TIER_CAND_K`
  （默认 5）——仅覆盖 DEFAULT_TIER_POLICY，用于扫参。

### 5. 测试

- core/tier_policy 纯函数单测：cap_for（tier1 优先/0=不限）、pick_first_allowed
  （递补次序/全触顶 None/held 边界 <cap）、effective_blacklist 保序去重。
- **同源守卫**：scheduler 剔锁输出 ≡ 用 pick_first_allowed(cap=1) 对同一 DB 状态
  的判定（防两侧各写各的）。
- 回测：K=1 逐位回归保真；K>1 行数≤K 且 rank 单调；分配器语义（tier1/OTHERS/
  边界释放/K 耗尽空过/stats 数字）；e2e 恒等/对比（tier2_cap=0 ≡ 无锁基线；
  cap=1 vs symbol_lock=True：**集合级包含均不成立**（分配路径依赖——递补币占用后续
  名额、饱和期 tiers 贪心早填/lock 稀疏后补，轮次交错，皆正确语义，实测确认），
  可比不变量=①总格数≥（贪心装箱不浪费名额）②产出同币锁窗无重叠）；缓存隔离
  （不同 K 不串）。
- 实盘重表达回归：scheduler/control_compute 既有测试全绿（行为不变）。

### 6. 评估用法（交付后标准跑法，不属实现范围）

四窗 × {无锁, cap=1 不递补(symbol_lock), cap=1 递补, cap=2 递补} ×
{tier0=∅, tier0=DEFAULT} → 每格收益/格数/递补统计对照表 → 决定实盘档1/档2
是否移植及参数（届时另出实盘方案批准）。

## 改动面

新：`core/tier_policy.py`。改：`config.py`（DEFAULT_TIER_POLICY + blacklist 默认
接线）、`runtime/scheduler.py` 与 `dashboard/control_compute.py`（剔锁重表达，
行为不变）、`backtest/backtest_run.py`（K 候选/分配器/接线/env）、
`deploy/fly.toml` `fly.prod.toml`（撤回长名单指向 config.py）。
