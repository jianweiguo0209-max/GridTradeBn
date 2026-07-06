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
