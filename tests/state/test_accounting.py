import pytest

from gridtrade.state.models import Accounting, ConcurrencyError


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.accounting import AccountingRepository
    s = StateStore.in_memory()
    s.create_all()
    return AccountingRepository(s)


def test_init_creates_zero_row():
    repo = _repo()
    a = repo.init('g1')
    assert a.grid_id == 'g1' and a.realized_pnl == 0.0 and a.version == 1
    # 幂等：再次 init 返回现有
    assert repo.init('g1').version == 1


def test_save_optimistic_lock():
    repo = _repo()
    a = repo.init('g1')
    a.realized_pnl = 12.5
    a.net_position = 3.0
    saved = repo.save(a)
    assert saved.realized_pnl == 12.5 and saved.version == 2
    # 用陈旧 version 再保存应失败
    stale = Accounting(grid_id='g1', version=1)
    with pytest.raises(ConcurrencyError):
        repo.save(stale)


def test_bump_peak_only_increases():
    repo = _repo()
    repo.init('g1')
    a1 = repo.bump_peak('g1', 0.02)
    assert a1.pnl_ratio_max == 0.02
    a2 = repo.bump_peak('g1', 0.01)   # 更低，不更新
    assert a2.pnl_ratio_max == 0.02
    a3 = repo.bump_peak('g1', 0.05)   # 新高
    assert a3.pnl_ratio_max == 0.05
