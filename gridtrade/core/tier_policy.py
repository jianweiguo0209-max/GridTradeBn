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
    # 杠杆感知上限(spec 2026-07-11-symbol-desk 组件四;2026-07-16 按币安杠杆刻度重标,用户定):币安
    # 最低杠杆 5x、无 ≤3x,故 HL 的 (3,1)(5,2) 在币安空转(每币仍 cap 2)。改 maxlev≤10 → cap 1,罩住
    # 币安最脆弱的 5x/10x 尾部(维持率高/薄盘;COIN-only 票池实测 14/614≈2.3% 币),不误伤 20x+ 主力;
    # 其余走 tier2_cap、取更严者。maxlev 由调用方注入(本层禁 import 交易所);None=不适用(向后兼容/金标)。
    lev_caps: tuple = ((10, 1),)


def effective_blacklist(blacklist, tiers) -> tuple:
    """档0 合并口径（单一事实源）：保序去重；tiers=None 原样透传。"""
    if tiers is None:
        return tuple(blacklist)
    return tuple(dict.fromkeys(tuple(blacklist) + tuple(tiers.tier0)))


def cap_for(symbol, tiers, maxlev=None) -> Optional[int]:
    """档1 名单 → 1；其余(OTHERS) → tier2_cap(0=不限);maxlev 给出时叠加杠杆感知档
    (lev_caps,取更严者)。扫描依据(2026-07-11 levcap):spec 档保费 −1.18pp/2mo,
    最差窗 MDD −3.35→−2.02、脆弱币堆叠 4→≤2。"""
    if symbol in tiers.tier1:
        return 1
    cap = tiers.tier2_cap if tiers.tier2_cap else None
    if maxlev is not None:
        for thr, c in getattr(tiers, 'lev_caps', ()) or ():
            if maxlev <= thr:
                cap = c if cap is None else min(cap, c)
                break
    return cap


def _allowed(symbol, held_counts, tiers, maxlev=None) -> bool:
    cap = cap_for(symbol, tiers, maxlev=maxlev)
    return cap is None or held_counts.get(symbol, 0) < cap


def pick_first_allowed(ranked_symbols, held_counts, tiers, maxlev_map=None) -> Optional[int]:
    """按序取第一个未触顶币的下标（=实盘方案A 与回测递补共用的唯一判定）；全触顶 → None。
    maxlev_map(可选):{symbol: maxlev},None=不启用杠杆感知(向后兼容/金标)。"""
    for i, sym in enumerate(ranked_symbols):
        ml = maxlev_map.get(sym) if maxlev_map else None
        if _allowed(sym, held_counts, tiers, maxlev=ml):
            return i
    return None


def capped_symbols(symbols, held_counts, tiers, maxlev_map=None) -> set:
    """已触顶币集合（实盘选币入口剔锁用；与 pick_first_allowed 同一 cap_for 派生）。"""
    out = set()
    for s in symbols:
        ml = maxlev_map.get(s) if maxlev_map else None
        if not _allowed(s, held_counts, tiers, maxlev=ml):
            out.add(s)
    return out
