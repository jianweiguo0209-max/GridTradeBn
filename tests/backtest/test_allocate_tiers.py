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
        _pick('2026-01-01 06:00', 6, 'A', 1),                 # tier1 触顶(1) 无备选 → 空过
        _pick('2026-01-01 12:00', 0, 'A'),                    # 恰满 period → 释放，允许
    ]
    out, stats = allocate_with_tiers(picks, tiers, period='12H')
    assert [str(rt) for rt, _, _ in out] == ['2026-01-01 00:00:00', '2026-01-01 12:00:00']
    assert stats['rejected_tier1'] == 1 and stats['empty_rounds'] == 1


def test_cap0_unlimited_identity():
    picks = [_pick('2026-01-01 %02d:00' % h, h, 'A') for h in range(5)]
    out, stats = allocate_with_tiers(picks, TierPolicy(tier2_cap=0), period='12H')
    assert len(out) == 5 and stats['empty_rounds'] == 0       # 不限 ≡ 全保留


def test_candidates_sorted_by_rank_not_input_order():
    # 候选行序无保证（select_grids 按 df 序发行）：分配器必须按 rank 排后递补
    tiers = TierPolicy(tier2_cap=1)
    picks = [
        _pick('2026-01-01 00:00', 0, 'A'),
        _pick('2026-01-01 01:00', 1, 'B', 2),                 # 故意先 rank2 后 rank1
        _pick('2026-01-01 01:00', 1, 'A', 1),                 # A 触顶 → 递补应选 B
    ]
    out, _ = allocate_with_tiers(picks, tiers, period='12H')
    assert [r['symbol'] for _, _, r in out] == ['A', 'B']
