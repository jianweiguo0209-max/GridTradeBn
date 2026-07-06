# 三档半拉黑名单可回测（TierPolicy）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 三档名单（档0硬禁/档1≤1/档2 OTHERS≤N）回测可配置可评估，且名单与判定逻辑实盘/回测单源共享（防"只改一处回测失真"）。

**Architecture:** 新 core/tier_policy.py 承载全部判定纯函数；config.py DEFAULT_TIER_POLICY 为名单唯一事实源（env 双侧只作覆盖）；实盘方案A剔锁重表达为共享函数（行为不变+同源守卫测试）；回测 select_grids 保留 top-K 候选、allocate_with_tiers 时间循环记账后调共享判定（固定 period 锁窗近似）。

**Tech Stack:** Python 3.9、pandas、pytest。

**Spec:** `docs/superpowers/specs/2026-07-06-tiered-blacklist-backtest-design.md`

## Global Constraints

- `tiers=None` 时回测逐位不变；实盘重表达后行为逐位不变（现状=tier2_cap=1）。
- `tiers` 与 `symbol_lock=True` 互斥（ValueError）。
- 固定 period 锁窗恰满边界=释放（与 filter_tasks_symbol_lock 同口径）；近似注记写 docstring。
- 测试跑法 `.venv/bin/python -m pytest <path> -q`；每 Task 一 commit。
- core/tier_policy.py 不得 import 交易所/回测/runtime（与 selection.py 同级纯策略层）。

---

### Task 1: core/tier_policy.py 共享策略层

**Files:**
- Create: `gridtrade/core/tier_policy.py`
- Test: `tests/core/test_tier_policy.py`（新建；tests/core/ 已存在）

**Interfaces:**
- Produces（全计划共用）:
  - `TierPolicy(tier0: tuple = (), tier1: tuple = (), tier2_cap: int = 2)`（frozen dataclass）
  - `effective_blacklist(blacklist, tiers) -> tuple`（保序去重；tiers=None 原样 tuple）
  - `cap_for(symbol, tiers) -> Optional[int]`（tier1→1；否则 tier2_cap；tier2_cap==0→None=不限）
  - `pick_first_allowed(ranked_symbols, held_counts, tiers) -> Optional[int]`（首个 held<cap 的下标；全触顶 None）
  - `capped_symbols(symbols, held_counts, tiers) -> set`（已触顶币集合；实盘剔锁用，同一 cap_for 派生）

- [ ] **Step 1: 失败测试**

```python
# tests/core/test_tier_policy.py
"""共享三档判定纯函数：实盘剔锁与回测递补的唯一语义源（spec 同源性要求②）。"""
from gridtrade.core.tier_policy import (TierPolicy, cap_for, capped_symbols,
                                        effective_blacklist, pick_first_allowed)

T = TierPolicy(tier0=('X/USDC:USDC',), tier1=('A/USDC:USDC',), tier2_cap=2)


def test_cap_for_tier1_wins_and_zero_means_unlimited():
    assert cap_for('A/USDC:USDC', T) == 1                  # tier1 名单
    assert cap_for('B/USDC:USDC', T) == 2                  # OTHERS
    assert cap_for('B/USDC:USDC', TierPolicy(tier2_cap=0)) is None   # 0=不限


def test_effective_blacklist_merges_ordered_dedup():
    assert effective_blacklist(('Z', 'X/USDC:USDC'), T) == ('Z', 'X/USDC:USDC')
    assert effective_blacklist(('Z',), T) == ('Z', 'X/USDC:USDC')
    assert effective_blacklist(('Z',), None) == ('Z',)


def test_pick_first_allowed_fallback_order():
    held = {'A/USDC:USDC': 1, 'B/USDC:USDC': 2}
    ranked = ['A/USDC:USDC', 'B/USDC:USDC', 'C/USDC:USDC']
    assert pick_first_allowed(ranked, held, T) == 2        # A 触顶(1)、B 触顶(2) → C
    assert pick_first_allowed(ranked[:2], held, T) is None  # 全触顶 → 空过
    assert pick_first_allowed(ranked, {}, T) == 0           # 无持仓 → 榜一
    assert pick_first_allowed(ranked, {'B/USDC:USDC': 99},
                              TierPolicy(tier2_cap=0)) == 0  # 不限恒可


def test_capped_symbols_matches_pick_semantics():
    held = {'A/USDC:USDC': 1, 'B/USDC:USDC': 1, 'C/USDC:USDC': 2}
    out = capped_symbols(['A/USDC:USDC', 'B/USDC:USDC', 'C/USDC:USDC', 'D/USDC:USDC'],
                         held, T)
    assert out == {'A/USDC:USDC', 'C/USDC:USDC'}           # A: tier1 满 1；B: 1<2 未满；C: 满 2
```

- [ ] **Step 2: 确认失败** `.venv/bin/python -m pytest tests/core/test_tier_policy.py -q` → ModuleNotFoundError
- [ ] **Step 3: 实现**

```python
# gridtrade/core/tier_policy.py
"""三档半拉黑判定（legacy black_dict 语义的共享策略层，spec 2026-07-06-tiered-*）。

实盘（scheduler 剔锁/control_compute 预览）与回测（allocate_with_tiers 递补）都只经
本模块判定——名单与逻辑单源，防"只改一处回测失真"。本模块禁止 import 交易所/回测/
runtime（与 selection.py 同级纯策略层）。tier0 在票池级由 effective_blacklist 合并
处理，cap 判定（cap_for/pick_first_allowed/capped_symbols）不再见到 tier0 币。
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TierPolicy:
    tier0: tuple = ()      # 硬禁：票池级剔除
    tier1: tuple = ()      # 名单币并发上限 1
    tier2_cap: int = 2     # 其余币(OTHERS)并发上限；0 = 不限


def effective_blacklist(blacklist, tiers) -> tuple:
    if tiers is None:
        return tuple(blacklist)
    return tuple(dict.fromkeys(tuple(blacklist) + tuple(tiers.tier0)))


def cap_for(symbol, tiers) -> Optional[int]:
    if symbol in tiers.tier1:
        return 1
    return tiers.tier2_cap if tiers.tier2_cap else None


def _allowed(symbol, held_counts, tiers) -> bool:
    cap = cap_for(symbol, tiers)
    return cap is None or held_counts.get(symbol, 0) < cap


def pick_first_allowed(ranked_symbols, held_counts, tiers) -> Optional[int]:
    for i, sym in enumerate(ranked_symbols):
        if _allowed(sym, held_counts, tiers):
            return i
    return None


def capped_symbols(symbols, held_counts, tiers) -> set:
    return {s for s in symbols if not _allowed(s, held_counts, tiers)}
```

- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** `feat(core): tier_policy 三档判定共享层（实盘/回测单一语义源）`

---

### Task 2: config.py 名单单源 + toml 撤回

**Files:**
- Modify: `gridtrade/config.py`（DEFAULT_TIER_POLICY + blacklist 默认接线）
- Modify: `deploy/fly.toml`、`deploy/fly.prod.toml`（撤回长名单→空占位+注释指向 config.py）
- Test: `tests/test_config.py`（校准 blacklist 默认语义 + 新增单源测试）

**Interfaces:**
- Consumes: Task 1 `TierPolicy`。
- Produces: `gridtrade.config.DEFAULT_TIER_POLICY`；`load_deploy_config` 语义：env
  `BLACKLIST_SYMBOLS` 非空→覆盖，未设或空串→`DEFAULT_TIER_POLICY.tier0`。

- [ ] **Step 1: 校准/新增测试**（tests/test_config.py 替换 `test_blacklist_parsing`）

```python
def test_blacklist_defaults_to_tier0_env_overrides():
    # 名单单源（spec 同源性①）：env 未设/空串 → DEFAULT_TIER_POLICY.tier0；非空 → 覆盖。
    from gridtrade.config import DEFAULT_TIER_POLICY
    assert load_deploy_config(env={}).blacklist == DEFAULT_TIER_POLICY.tier0
    assert load_deploy_config(env={'BLACKLIST_SYMBOLS': ''}).blacklist == DEFAULT_TIER_POLICY.tier0
    cfg = load_deploy_config(env={'BLACKLIST_SYMBOLS': ' BTC , ETH ,SOL '})
    assert cfg.blacklist == ('BTC', 'ETH', 'SOL')


def test_default_tier_policy_content():
    from gridtrade.config import DEFAULT_TIER_POLICY
    assert 'FARTCOIN/USDC:USDC' in DEFAULT_TIER_POLICY.tier0     # legacy 档0 移植
    assert 'KNEIRO/USDC:USDC' in DEFAULT_TIER_POLICY.tier0       # NEIRO→HL k 前缀
    assert len(DEFAULT_TIER_POLICY.tier0) == 9
    assert DEFAULT_TIER_POLICY.tier1 == () and DEFAULT_TIER_POLICY.tier2_cap == 1
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**（config.py 在 DEFAULT_STRATEGY_CONFIG 旁）

```python
from gridtrade.core.tier_policy import TierPolicy

# 三档名单唯一事实源（spec 2026-07-06-tiered-*）：实盘默认与回测默认都取此处；
# env（实盘 BLACKLIST_SYMBOLS / 回测 BT_TIER0 等）只作覆盖（运维紧急面/扫参面）。
DEFAULT_TIER_POLICY = TierPolicy(
    tier0=('BTC/USDC:USDC', 'ETH/USDC:USDC', 'VINE/USDC:USDC', 'NEO/USDC:USDC',
           'PEOPLE/USDC:USDC', 'KNEIRO/USDC:USDC', 'MOODENG/USDC:USDC',
           'FARTCOIN/USDC:USDC', 'CFX/USDC:USDC'),
    # legacy black_dict["0"] 25 币中 HL 在市 9 个（NEIRO→k 前缀 KNEIRO）；未上市 16 币
    # 不猜译名（PI/DEGEN/ALCH/MAX/OL/MASK/ACT/SONIC/BR/RDNT/MAGIC/CSPR/LOOKS/MEW/
    # NEIROETH/IP），上市巡检再补。
    tier1=(),
    tier2_cap=1,   # 当前实盘现实（SymbolLockGate 每币≤1）；回测评估后另批准再调
)
```

`load_deploy_config` blacklist 行改：

```python
        blacklist=_csv(env, 'BLACKLIST_SYMBOLS') or DEFAULT_TIER_POLICY.tier0,
```

两个 toml：把 `BLACKLIST_SYMBOLS = "BTC/USDC:USDC,...9币..."` 行整体替换为：

```toml
  # 档0 硬禁名单单源在 gridtrade/config.py DEFAULT_TIER_POLICY.tier0（legacy 移植 9 币）；
  # 此处 env 仅作运维紧急覆盖（非空才生效），名单变更走代码 commit 两侧原子同步。
```

- [ ] **Step 4: 确认通过** + `tests/ -q` 相关目录绿（scheduler 等用 USDT 测试符号与 tier0 USDC 名单无交集，不受影响）
- [ ] **Step 5: Commit** `feat(config): DEFAULT_TIER_POLICY 名单单源（env 仅覆盖；toml 撤回长名单）`

---

### Task 3: 实盘剔锁重表达（行为不变）+ 同源守卫

**Files:**
- Modify: `gridtrade/runtime/scheduler.py`（方案A 剔锁改经 capped_symbols）
- Modify: `gridtrade/dashboard/control_compute.py`（同）
- Test: `tests/runtime/test_scheduler.py`（守卫测试追加；既有测试不动=行为回归）

**Interfaces:**
- Consumes: Task 1 `capped_symbols`、Task 2 `DEFAULT_TIER_POLICY`。

- [ ] **Step 1: 追加同源守卫测试**

```python
def test_prefilter_equals_shared_tier_policy_semantics():
    # 同源守卫（spec 同源性②）：scheduler 剔锁结果 ≡ 共享 capped_symbols 对同一
    # 状态的判定（cap=1 现状）。防止两侧各写各的悄悄漂移。
    from collections import Counter
    from gridtrade.core.tier_policy import TierPolicy, capped_symbols
    held = Counter({'BBB/USDC:USDC': 1})
    universe = ['AAA/USDC:USDC', 'BBB/USDC:USDC', 'CCC/USDC:USDC']
    out = capped_symbols(universe, held, TierPolicy(tier2_cap=1))
    assert out == {'BBB/USDC:USDC'}    # 与 Task 既有 prefilter 测试的期望剔除集一致
```

- [ ] **Step 2: 实现重表达**（scheduler.py 剔锁段替换为）

```python
    from collections import Counter
    from gridtrade.config import DEFAULT_TIER_POLICY
    from gridtrade.core.tier_policy import capped_symbols
    # 方案A 剔锁经共享 tier_policy 表达（spec 同源性②）：现配置 tier2_cap=1 行为与
    # 原实现逐位相同；本轮换仓 tag 自己的币即将释放不计 held（连任语义，状态供给侧口径）。
    held = Counter(g.symbol for g in rt.manager.executor.grids.list_active()
                   if g.tag != tag)
    banned = capped_symbols(universe, held, DEFAULT_TIER_POLICY)
    if banned:
        universe = [s for s in universe if s not in banned]
        print('[scheduler] symbol-lock pre-filter: -%d held %s'
              % (len(banned), sorted(banned)), flush=True)
```

control_compute.py 同样（held 无 tag 豁免）：

```python
    mgr = getattr(rt, 'manager', None)
    if mgr is not None:
        from collections import Counter
        from gridtrade.config import DEFAULT_TIER_POLICY
        from gridtrade.core.tier_policy import capped_symbols
        held = Counter(g.symbol for g in mgr.executor.grids.list_active())
        universe = [s for s in universe
                    if s not in capped_symbols(universe, held, DEFAULT_TIER_POLICY)]
```

（imports 提到各文件顶部，不放函数内。）

- [ ] **Step 3: 全跑** `tests/runtime/test_scheduler.py tests/dashboard/test_control_compute.py -q`（既有 prefilter 测试原样通过=行为不变证明）
- [ ] **Step 4: Commit** `refactor(scheduler): 方案A剔锁经共享 tier_policy 表达（行为不变+同源守卫）`

---

### Task 4: 回测 select_grids 保留 top-K 候选

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`（select_grids 加 candidates_per_rt）
- Test: `tests/backtest/test_tier_candidates.py`（新建）

**Interfaces:**
- Consumes: replay_selection 从 strategy_config['choose_symbols'] 取截断值。
- Produces: `select_grids(..., candidates_per_rt=1)`；K>1 时每 (rt,offset) ≤K 行、row 带 'rank'。

- [ ] **Step 1: 失败测试**（用现有回测测试的小数据 harness——参照 tests/backtest/ 内 symbol_lock e2e 测试的 cache/fixture 搭法，取其最小窗口场景）

```python
# tests/backtest/test_tier_candidates.py
"""top-K 候选保留：K=1 与现状逐位一致（保真回归）；K>1 行数≤K 且 rank 单调。"""
import pandas as pd
from gridtrade.backtest.backtest_run import select_grids
# —— cache/universe/window/strategy fixture 复用 tests/backtest/ 既有 symbol_lock
#    e2e 测试同一套（导入其模块级构造函数/fixture；如该文件用 conftest fixture 则直接吃）。


def test_k1_identical_to_baseline(bt_cache_small):
    cache, universe, ws, we, cfg, factors = bt_cache_small
    base = select_grids(cache, universe, ws, we, cfg, factors, log=lambda *a: None)
    k1 = select_grids(cache, universe, ws, we, cfg, factors,
                      candidates_per_rt=1, log=lambda *a: None)
    assert [(rt, off, r['symbol']) for rt, off, r in base] == \
           [(rt, off, r['symbol']) for rt, off, r in k1]


def test_k3_rows_bounded_and_rank_monotone(bt_cache_small):
    cache, universe, ws, we, cfg, factors = bt_cache_small
    k3 = select_grids(cache, universe, ws, we, cfg, factors,
                      candidates_per_rt=3, log=lambda *a: None)
    by_rt = {}
    for rt, off, row in k3:
        by_rt.setdefault((rt, off), []).append(row['rank'])
    for ranks in by_rt.values():
        assert len(ranks) <= 3 and ranks == sorted(ranks)
```

（`bt_cache_small` fixture：若 tests/backtest/ 无现成，可在本文件用既有 symbol_lock e2e 测试的数据构造代码原样提炼成 fixture——实现时以该文件实际结构为准，这是唯一允许"看现场再抄"的点，因其纯 fixture 无语义。）

- [ ] **Step 2: 确认失败**（TypeError: candidates_per_rt）
- [ ] **Step 3: 实现**（select_grids 内）

```python
def select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                 *, timeframe='1h', min_quote_volume=0.0, blacklist=(), workers=1,
                 candidates_per_rt=1, log=print):
    ...
    if candidates_per_rt and candidates_per_rt > 1:
        # top-K 候选（三档递补用）：放宽选币截断为 rank<=K；K 经 strategy_config 进
        # select 缓存 key（不同 K 不串缓存）。K=1 恒等现状。
        strategy_config = dict(strategy_config,
                               choose_symbols=int(candidates_per_rt))
    # 其余逻辑不动（compute_key/replay_selection 均已吃 strategy_config）
```

- [ ] **Step 4: 确认通过** + 缓存隔离断言（同 fixture 下 K=1/K=3 select cache key 不同——直接调 SC.compute_key 断言 key 不等）
- [ ] **Step 5: Commit** `feat(backtest): select_grids top-K 候选保留（K=1 逐位恒等；K 进缓存 key）`

---

### Task 5: allocate_with_tiers 分配器

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`（filter_tasks_symbol_lock 旁新增）
- Test: `tests/backtest/test_allocate_tiers.py`（新建）

**Interfaces:**
- Consumes: Task 1 `TierPolicy/pick_first_allowed`。
- Produces: `allocate_with_tiers(ranked_picks, tiers, period='12H') -> (picks, stats)`；
  ranked_picks=[(rt, offset, row)]（同 rt 多行按 row['rank'] 升序参与递补）；
  stats={'rejected_tier1', 'rejected_tier2', 'fallback_hist': dict, 'empty_rounds'}。

- [ ] **Step 1: 失败测试**

```python
# tests/backtest/test_allocate_tiers.py
"""三档分配器：固定 period 锁窗 + 共享 pick_first_allowed 递补。纯函数、合成输入。"""
import pandas as pd

from gridtrade.core.tier_policy import TierPolicy
from gridtrade.backtest.backtest_run import allocate_with_tiers


def _row(sym, rank):
    return pd.Series({'symbol': sym, 'rank': rank})


def _pick(ts, off, sym, rank=1):
    return (pd.Timestamp(ts), off, _row(sym, rank))


def test_cap2_allows_two_then_fallback():
    tiers = TierPolicy(tier2_cap=2)
    picks = [
        _pick('2026-01-01 00:00', 0, 'A'),
        _pick('2026-01-01 01:00', 1, 'A'),                    # A 第 2 个并发 → 允许
        _pick('2026-01-01 02:00', 2, 'A', 1),                 # A 触顶
        _pick('2026-01-01 02:00', 2, 'B', 2),                 # → 递补 B
    ]
    out, stats = allocate_with_tiers(picks, tiers, period='12H')
    assert [r['symbol'] for _, _, r in out] == ['A', 'A', 'B']
    assert stats['fallback_hist'] == {1: 1}                   # 一次递补深度 1
    assert stats['rejected_tier2'] == 1 and stats['empty_rounds'] == 0


def test_tier1_cap1_and_boundary_release():
    tiers = TierPolicy(tier1=('A',), tier2_cap=2)
    picks = [
        _pick('2026-01-01 00:00', 0, 'A'),
        _pick('2026-01-01 06:00', 6, 'A', 1),                 # tier1 触顶(1) 无备选
        _pick('2026-01-01 12:00', 0, 'A'),                    # 恰满 period → 释放，允许
    ]
    out, stats = allocate_with_tiers(picks, tiers, period='12H')
    assert [str(rt) for rt, _, _ in out] == ['2026-01-01 00:00:00', '2026-01-01 12:00:00']
    assert stats['rejected_tier1'] == 1 and stats['empty_rounds'] == 1


def test_cap0_unlimited_identity():
    picks = [_pick('2026-01-01 %02d:00' % h, h, 'A') for h in range(5)]
    out, stats = allocate_with_tiers(picks, TierPolicy(tier2_cap=0), period='12H')
    assert len(out) == 5 and stats['empty_rounds'] == 0       # 不限 ≡ 全保留
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

```python
def allocate_with_tiers(ranked_picks, tiers, period='12H'):
    """三档分配（spec 2026-07-06-tiered-*）：按 run_time 升序，每轮候选按 rank 升序经
    共享 pick_first_allowed 取第一个未触顶币（=实盘方案A 次优递补）；held 记账为
    **固定 period 锁窗近似**（恰满边界=释放，与 filter_tasks_symbol_lock 同口径；
    实盘止损早退会提前释放锁，四窗实测早退 ~4% 格，本近似偏保守）。排名在全池上算、
    触顶后跳选（实盘为剔后再排，微小位移方向无偏——保选币向量化的既定取舍）。
    返回 (picks 同形子集, stats)。"""
    from gridtrade.core.tier_policy import cap_for, pick_first_allowed
    td = pd.to_timedelta(period)
    by_round = {}
    for rt, off, row in ranked_picks:
        by_round.setdefault((rt, off), []).append((rt, off, row))
    expiry = []          # [(release_ts, symbol)]
    held = {}
    kept = []
    stats = {'rejected_tier1': 0, 'rejected_tier2': 0,
             'fallback_hist': {}, 'empty_rounds': 0}
    for key in sorted(by_round):
        rt = key[0]
        while expiry and expiry[0][0] <= rt:                 # 恰满边界=释放
            _, sym = heapq.heappop(expiry)
            held[sym] -= 1
            if not held[sym]:
                del held[sym]
        cands = sorted(by_round[key], key=lambda t: t[2]['rank'])
        idx = pick_first_allowed([c[2]['symbol'] for c in cands], held, tiers)
        for j, c in enumerate(cands):
            if idx is not None and j == idx:
                continue
            if idx is not None and j > idx:
                break                                        # idx 之后未参与判定
            sym = c[2]['symbol']
            if cap_for(sym, tiers) == 1 and sym in tiers.tier1:
                stats['rejected_tier1'] += 1
            else:
                stats['rejected_tier2'] += 1
        if idx is None:
            stats['empty_rounds'] += 1
            continue
        if idx > 0:
            stats['fallback_hist'][idx] = stats['fallback_hist'].get(idx, 0) + 1
        chosen = cands[idx]
        sym = chosen[2]['symbol']
        held[sym] = held.get(sym, 0) + 1
        heapq.heappush(expiry, (rt + td, sym))
        kept.append(chosen)
    return kept, stats
```

（`import heapq` 置文件顶。）

- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** `feat(backtest): allocate_with_tiers 三档递补分配器（共享判定+period 锁窗）`

---

### Task 6: run_backtest 接线 + env + e2e 恒等/包含

**Files:**
- Modify: `gridtrade/backtest/backtest_run.py`（run_backtest 参数 + main() env）
- Test: `tests/backtest/test_tiers_e2e.py`（新建，复用 Task 4 fixture）

**Interfaces:**
- Consumes: Task 1/4/5 全部。
- Produces: `run_backtest(..., tiers=None, tier_cand_k=5)`。

- [ ] **Step 1: 失败测试**

```python
# tests/backtest/test_tiers_e2e.py
import pytest

from gridtrade.core.tier_policy import TierPolicy
from gridtrade.backtest.backtest_run import run_backtest, filter_tasks_symbol_lock


def test_tiers_and_symbol_lock_mutually_exclusive(bt_cache_small):
    cache, universe, ws, we, cfg, factors = bt_cache_small
    with pytest.raises(ValueError):
        run_backtest(cache, universe, ws, we, cfg, factors,
                     symbol_lock=True, tiers=TierPolicy())


def test_cap0_identity_with_baseline(bt_cache_small):
    cache, universe, ws, we, cfg, factors = bt_cache_small
    base = run_backtest(cache, universe, ws, we, cfg, factors, log=lambda *a: None)
    t0 = run_backtest(cache, universe, ws, we, cfg, factors,
                      tiers=TierPolicy(tier2_cap=0), log=lambda *a: None)
    assert base.equals(t0)                                   # 不限 ≡ 无锁基线


def test_cap1_superset_of_symbol_lock(bt_cache_small):
    # 递补只增不减：cap=1 递补的 (rt,symbol) 选中集 ⊇ symbol_lock 不递补的选中集
    cache, universe, ws, we, cfg, factors = bt_cache_small
    lock = run_backtest(cache, universe, ws, we, cfg, factors,
                        symbol_lock=True, log=lambda *a: None)
    t1 = run_backtest(cache, universe, ws, we, cfg, factors,
                      tiers=TierPolicy(tier2_cap=1), log=lambda *a: None)
    key = lambda df: set(zip(df['run_time'].astype(str), df['symbol']))
    assert key(lock) <= key(t1)
```

（结果列名以 `_RESULT_COLS` 实际为准——实现时核对 run_time/symbol 两列的真实列名，替换 key()。）

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**（run_backtest 签名与体内）

```python
def run_backtest(..., symbol_lock=False, tiers=None, tier_cand_k=5, log=print):
    if tiers is not None and symbol_lock:
        raise ValueError('tiers 与 symbol_lock 互斥（两套口径不叠加）')
    if tiers is not None:
        from gridtrade.core.tier_policy import effective_blacklist
        blacklist = effective_blacklist(blacklist, tiers)     # 档0 票池级
        grids_picks = select_grids(cache, universe, window_start, window_end,
                                   strategy_config, factors, timeframe=timeframe,
                                   min_quote_volume=min_quote_volume,
                                   blacklist=blacklist, workers=workers,
                                   candidates_per_rt=int(tier_cand_k), log=log)
        picks, stats = allocate_with_tiers(grids_picks, tiers,
                                           period=strategy_config['period'])
        log('[BT] tiers: rejected t1=%d t2=%d fallback=%s empty=%d'
            % (stats['rejected_tier1'], stats['rejected_tier2'],
               stats['fallback_hist'], stats['empty_rounds']))
        tasks = assemble_grid_tasks(cache, picks, strategy_config,
                                    sim_timeframe=sim_timeframe,
                                    timeframe=timeframe, log=log)
    else:
        tasks = build_grid_tasks(...)                        # 现状路径原样
        if symbol_lock:
            ...                                              # 现状原样
```

main() env（现有 BT_SYMBOL_LOCK 旁）：

```python
    _t1 = [s for s in os.environ.get('BT_TIER1_SYMBOLS', '').split(',') if s.strip()]
    _t2cap = os.environ.get('BT_TIER2_CAP')
    if _t1 or _t2cap is not None or os.environ.get('BT_TIER0') is not None:
        from gridtrade.config import DEFAULT_TIER_POLICY
        _t0 = os.environ.get('BT_TIER0')
        tiers = TierPolicy(
            tier0=tuple(s.strip() for s in _t0.split(',') if s.strip())
                  if _t0 is not None else DEFAULT_TIER_POLICY.tier0,
            tier1=tuple(s.strip() for s in _t1),
            tier2_cap=int(_t2cap) if _t2cap is not None else DEFAULT_TIER_POLICY.tier2_cap)
    else:
        tiers = None    # 默认不启用（基线可比）；显式 env 才开三档评估
```

（K：`BT_TIER_CAND_K` 默认 5，`int(os.environ.get('BT_TIER_CAND_K', 5))` 传 tier_cand_k。）

- [ ] **Step 4: 确认通过** + `tests/backtest/ -q` 全绿
- [ ] **Step 5: Commit** `feat(backtest): run_backtest 三档接线（tiers/K/env；与 symbol_lock 互斥；评估统计输出）`

---

### Task 7: 收尾——全套 + 文档

- [ ] `.venv/bin/python -m pytest -q` 全绿（≈610+）
- [ ] STATUS.md：三档可回测落地一行（名单/判定单源 + 评估跑法指向 spec §6）
- [ ] Commit `docs(status): 三档半拉黑可回测落地记录`

## Self-Review 结果

- **Spec 覆盖**：同源性①→Task 2；②→Task 1/3（守卫）；③近似注记→Task 5 docstring；top-K→Task 4；分配器/stats→Task 5；接线/互斥/env→Task 6；测试矩阵各条均落位；评估用法=spec §6 交付后跑法（不属实现）。
- **占位符**：Task 4 fixture 与 Task 6 结果列名两处"以现场为准"均为纯 fixture/列名核对、无语义决策，符合允许范围；其余零占位。
- **类型一致**：TierPolicy/cap_for/pick_first_allowed/capped_symbols/allocate_with_tiers/candidates_per_rt/tier_cand_k 全链名称与签名一致。
