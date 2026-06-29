from gridtrade.state.models import Fill


def _repo(store):
    from gridtrade.state.fills import FillRepository
    return FillRepository(store)


def _fill(trade_id='t1', **kw):
    base = dict(trade_id=trade_id, grid_id='g1', line_index=5, side='sell',
                price=100.5, size=0.5, ts=1000)
    base.update(kw)
    return Fill(**base)


def test_add_if_new_dedup(store):
    repo = _repo(store)
    assert repo.add_if_new(_fill('t1')) is True
    assert repo.add_if_new(_fill('t1')) is False    # 同 trade_id 第二次 → False
    assert len(repo.list_by_grid('g1')) == 1


def test_list_by_grid_sorted_by_ts(store):
    repo = _repo(store)
    repo.add_if_new(_fill('t3', ts=3000))
    repo.add_if_new(_fill('t1', ts=1000))
    repo.add_if_new(_fill('t2', ts=2000))
    assert [f.ts for f in repo.list_by_grid('g1')] == [1000, 2000, 3000]


def test_max_ts(store):
    repo = _repo(store)
    assert repo.max_ts('g1') == 0
    repo.add_if_new(_fill('t1', ts=1000))
    repo.add_if_new(_fill('t2', ts=5000))
    assert repo.max_ts('g1') == 5000
    assert repo.max_ts('other') == 0


def test_fee_persisted_and_read_back(store):
    repo = _repo(store)
    assert repo.add_if_new(_fill('tf', fee=1.23)) is True
    got = repo.list_by_grid('g1')
    assert len(got) == 1
    assert abs(got[0].fee - 1.23) < 1e-12


def test_fee_defaults_zero_when_omitted(store):
    repo = _repo(store)
    repo.add_if_new(_fill('t0'))          # _fill 不传 fee → Fill.fee 默认 0.0
    assert repo.list_by_grid('g1')[0].fee == 0.0
