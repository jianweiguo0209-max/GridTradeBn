# tests/backtest/test_tier_candidates.py
"""top-K 候选保留：K=1 与现状逐位一致（保真回归）；K>1 行数≤K 且 rank 单调；缓存隔离。"""
import pandas as pd

from tests.backtest.test_selection_replay import _seed_cache, STRAT, FACTORS
from gridtrade.backtest.backtest_run import select_grids

WS = pd.Timestamp('2024-01-10 00:00:00')
WE = pd.Timestamp('2024-01-11 00:00:00')
SYMS = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']


def _quiet(*a, **k):
    pass


def test_k1_identical_to_baseline(tmp_path):
    cache = _seed_cache(tmp_path, SYMS)
    base = select_grids(cache, SYMS, WS, WE, STRAT, FACTORS, log=_quiet)
    k1 = select_grids(cache, SYMS, WS, WE, STRAT, FACTORS,
                      candidates_per_rt=1, log=_quiet)
    assert [(rt, off, r['symbol']) for rt, off, r in base] == \
           [(rt, off, r['symbol']) for rt, off, r in k1]
    assert base                                       # 场景非空（防真空通过）


def test_k3_rows_bounded_and_rank_monotone(tmp_path):
    cache = _seed_cache(tmp_path, SYMS)
    k3 = select_grids(cache, SYMS, WS, WE, STRAT, FACTORS,
                      candidates_per_rt=3, log=_quiet)
    by_rt = {}
    for rt, off, row in k3:
        by_rt.setdefault((rt, off), []).append(float(row['rank']))
    assert any(len(v) > 1 for v in by_rt.values())    # 确实保留了次优候选
    for ranks in by_rt.values():
        # 行序不保证按 rank（回放按 df 序发行；排序职责在 allocate_with_tiers）——
        # 只断言集合正确：≤K 行、rank 为从 1 起的连续名次
        assert len(ranks) <= 3
        assert sorted(ranks) == [float(i) for i in range(1, len(ranks) + 1)]


def test_k_enters_select_cache_key(tmp_path):
    from gridtrade.backtest import select_cache as SC
    cache = _seed_cache(tmp_path, SYMS)
    k1, _ = SC.compute_key(cache, SYMS, WS, WE, '1h', 0.0, (), dict(STRAT), FACTORS)
    k3, _ = SC.compute_key(cache, SYMS, WS, WE, '1h', 0.0, (),
                           dict(STRAT, choose_symbols=3), FACTORS)
    assert k1 != k3                                   # 不同 K 不串缓存
