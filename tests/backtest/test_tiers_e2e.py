# tests/backtest/test_tiers_e2e.py
"""run_backtest 三档接线 e2e：互斥、cap0 恒等基线、cap1 递补 ⊇ symbol_lock 不递补。"""
import pandas as pd
import pytest

from tests.backtest.test_selection_replay import _seed_cache, STRAT, FACTORS
from tests.backtest.test_backtest_run import _strategy
from gridtrade.core.tier_policy import TierPolicy
from gridtrade.backtest.backtest_run import run_backtest

WS = pd.Timestamp('2024-01-10 00:00:00')
WE = pd.Timestamp('2024-01-11 00:00:00')
SYMS = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']


def _quiet(*a, **k):
    pass


def test_tiers_and_symbol_lock_mutually_exclusive(tmp_path):
    cache = _seed_cache(tmp_path, SYMS)
    with pytest.raises(ValueError):
        run_backtest(cache, SYMS, WS, WE, _strategy(), FACTORS,
                     symbol_lock=True, tiers=TierPolicy())


def test_cap0_identity_with_baseline(tmp_path):
    cache = _seed_cache(tmp_path, SYMS)
    base = run_backtest(cache, SYMS, WS, WE, _strategy(), FACTORS, log=_quiet)
    t0 = run_backtest(cache, SYMS, WS, WE, _strategy(), FACTORS,
                      tiers=TierPolicy(tier2_cap=0), log=_quiet)
    assert len(base) > 0
    key = lambda df: sorted(zip(df['run_time'].astype(str), df['symbol']))
    assert key(base) == key(t0)                        # 不限 ≡ 无锁基线（同一批网格）


def test_cap1_beats_symbol_lock_and_respects_cap(tmp_path):
    # 分配路径依赖：递补开出的币占用后续名额，(rt,symbol) 乃至轮次集合与 lock 版
    # 互不包含（饱和期 tiers 贪心早填满、lock 稀疏后补——两者轮次交错，皆为正确语义）。
    # 可比的不变量只有两条：①总格数 ≥（贪心装箱不浪费任何可用名额）；
    # ②产出满足并发上限（同币锁窗 [rt, rt+period) 无重叠）。
    cache = _seed_cache(tmp_path, SYMS)
    lock = run_backtest(cache, SYMS, WS, WE, _strategy(), FACTORS,
                        symbol_lock=True, log=_quiet)
    t1 = run_backtest(cache, SYMS, WS, WE, _strategy(), FACTORS,
                      tiers=TierPolicy(tier2_cap=1), log=_quiet)
    assert len(lock) > 0 and len(t1) >= len(lock)
    td = pd.Timedelta('12H')
    for sym, grp in t1.groupby('symbol'):
        ts = sorted(pd.to_datetime(grp['run_time']))
        for a, b in zip(ts, ts[1:]):
            assert b >= a + td                     # cap=1：同币锁窗不重叠
