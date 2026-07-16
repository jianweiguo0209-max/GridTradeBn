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


def test_lev_caps_tiers():
    """cap_for 杠杆感知逻辑(spec 2026-07-11-symbol-desk 组件四):显式 lev_caps=((3,1),(5,2)) 下
    maxlev≤3→1、≤5→2、其余→tier2_cap;None=不启用(向后兼容);与 tier2_cap 取更严者。
    (纯逻辑测试用显式 lev_caps,独立于生产默认——见 test_lev_caps_default_binance_calibrated。)"""
    from gridtrade.core.tier_policy import TierPolicy, cap_for, capped_symbols
    LC = ((3, 1), (5, 2))
    tp = TierPolicy(tier2_cap=4, lev_caps=LC)
    s = 'X/USDC:USDC'
    assert cap_for(s, tp, maxlev=3) == 1
    assert cap_for(s, tp, maxlev=5) == 2
    assert cap_for(s, tp, maxlev=20) == 4
    assert cap_for(s, tp) == 4                      # None → 原行为
    assert cap_for(s, TierPolicy(tier2_cap=1, lev_caps=LC), maxlev=5) == 1   # 取更严
    # map 注入:lev3 币持 1 格即触顶;高杠杆币持 3 格不触
    held = {'A/USDC:USDC': 1, 'B/USDC:USDC': 3}
    mm = {'A/USDC:USDC': 3.0, 'B/USDC:USDC': 20.0}
    assert capped_symbols(['A/USDC:USDC', 'B/USDC:USDC'], held, tp, maxlev_map=mm) \
        == {'A/USDC:USDC'}
    assert capped_symbols(['A/USDC:USDC'], held, tp) == set()   # 无 map → 原行为


def test_lev_caps_default_binance_calibrated():
    """生产默认按币安杠杆刻度重标(2026-07-16):币安最低杠杆 5x、无 ≤3x,故 maxlev≤10 → cap 1
    (罩住 5x/10x 脆弱尾部,COIN-only 票池 14/614≈2.3% 币),其余(>10x)走 tier2_cap。"""
    from gridtrade.core.tier_policy import cap_for
    from gridtrade.config import DEFAULT_TIER_POLICY as P
    assert P.lev_caps == ((10, 1),)
    s = 'X/USDT:USDT'
    assert cap_for(s, P, maxlev=5) == 1       # 币安最脆弱
    assert cap_for(s, P, maxlev=10) == 1      # ≤10 上界仍收紧到 1
    assert cap_for(s, P, maxlev=20) == 2      # >10 → tier2_cap
    assert cap_for(s, P, maxlev=125) == 2
    assert cap_for(s, P, maxlev=None) == 2    # 未知 → 原行为(不收紧)
