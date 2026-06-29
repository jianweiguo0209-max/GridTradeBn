from gridtrade.state.models import Record


def _repo(store):
    from gridtrade.state.records import RecordRepository
    return RecordRepository(store)


def _rec(**kw):
    base = dict(id='', exchange='okx', symbol='BTC/USDT:USDT', tag='acc0at0')
    base.update(kw)
    return Record(**base)


def test_add_assigns_id_and_created_at(store):
    repo = _repo(store)
    r = repo.add(_rec(total_pnl=5.0, pnl_ratio=0.01, exit_reason='固定止损'))
    assert r.id and r.created_at > 0
    got = repo.get(r.id)
    assert got.total_pnl == 5.0 and got.exit_reason == '固定止损'


def test_list_by_tag_and_grid(store):
    repo = _repo(store)
    repo.add(_rec(tag='acc0at0', grid_id='g1'))
    repo.add(_rec(tag='acc0at0', grid_id='g2'))
    repo.add(_rec(tag='acc0at1', grid_id='g3'))
    assert len(repo.list_by_tag('acc0at0')) == 2
    assert len(repo.list_by_tag('acc0at1')) == 1
    assert {r.grid_id for r in repo.list_by_grid('g1')} == {'g1'}


def test_get_missing_returns_none(store):
    assert _repo(store).get('nope') is None
