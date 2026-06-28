from gridtrade.state.models import Fill


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.fills import FillRepository
    s = StateStore.in_memory(); s.create_all()
    return FillRepository(s)


def _fill(trade_id='t1', **kw):
    base = dict(trade_id=trade_id, grid_id='g1', line_index=5, side='sell',
                price=100.5, size=0.5, ts=1000)
    base.update(kw)
    return Fill(**base)


def test_add_if_new_dedup():
    repo = _repo()
    assert repo.add_if_new(_fill('t1')) is True
    assert repo.add_if_new(_fill('t1')) is False    # 同 trade_id 第二次 → False
    assert len(repo.list_by_grid('g1')) == 1


def test_list_by_grid_sorted_by_ts():
    repo = _repo()
    repo.add_if_new(_fill('t3', ts=3000))
    repo.add_if_new(_fill('t1', ts=1000))
    repo.add_if_new(_fill('t2', ts=2000))
    assert [f.ts for f in repo.list_by_grid('g1')] == [1000, 2000, 3000]


def test_max_ts():
    repo = _repo()
    assert repo.max_ts('g1') == 0
    repo.add_if_new(_fill('t1', ts=1000))
    repo.add_if_new(_fill('t2', ts=5000))
    assert repo.max_ts('g1') == 5000
    assert repo.max_ts('other') == 0
