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
    """档0 合并口径（单一事实源）：保序去重；tiers=None 原样透传。"""
    if tiers is None:
        return tuple(blacklist)
    return tuple(dict.fromkeys(tuple(blacklist) + tuple(tiers.tier0)))


def cap_for(symbol, tiers) -> Optional[int]:
    """档1 名单 → 1；其余(OTHERS) → tier2_cap；tier2_cap==0 → None=不限。"""
    if symbol in tiers.tier1:
        return 1
    return tiers.tier2_cap if tiers.tier2_cap else None


def _allowed(symbol, held_counts, tiers) -> bool:
    cap = cap_for(symbol, tiers)
    return cap is None or held_counts.get(symbol, 0) < cap


def pick_first_allowed(ranked_symbols, held_counts, tiers) -> Optional[int]:
    """按序取第一个未触顶币的下标（=实盘方案A 与回测递补共用的唯一判定）；全触顶 → None。"""
    for i, sym in enumerate(ranked_symbols):
        if _allowed(sym, held_counts, tiers):
            return i
    return None


def capped_symbols(symbols, held_counts, tiers) -> set:
    """已触顶币集合（实盘选币入口剔锁用；与 pick_first_allowed 同一 cap_for 派生）。"""
    return {s for s in symbols if not _allowed(s, held_counts, tiers)}
