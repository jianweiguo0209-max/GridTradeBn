import pytest

from gridtrade.state.models import (Grid, ACTIVE, OPENING, CLOSED, CLOSING,
                                    PENDING, ConcurrencyError, StateError)


def _repo(store):
    from gridtrade.state.grids import GridRepository
    return GridRepository(store)


def _grid(**kw):
    base = dict(id='', exchange='okx', symbol='BTC/USDT:USDT', status=PENDING)
    base.update(kw)
    return Grid(**base)


def test_create_assigns_id_and_timestamps(store):
    repo = _repo(store)
    g = repo.create(_grid())
    assert g.id and g.created_at > 0 and g.updated_at > 0 and g.version == 1
    assert repo.get(g.id).symbol == 'BTC/USDT:USDT'


def test_get_active_by_symbol(store):
    repo = _repo(store)
    g = repo.create(_grid(status=ACTIVE))
    found = repo.get_active_by_symbol('okx', 'BTC/USDT:USDT')
    assert found is not None and found.id == g.id
    assert repo.get_active_by_symbol('okx', 'ETH/USDT:USDT') is None


def test_active_same_symbol_capped_at_slot_limit(store):
    # 槽位方案语义校准（原为 UNIQUE 直接拒第 2 格）：cap 内可开多格、槽满抛
    # ConcurrencyError（DB 级兜底不丢，详见 tests/state/test_grid_slots.py）。
    from gridtrade.state.models import ConcurrencyError
    repo = _repo(store)
    repo.create(_grid(status=ACTIVE), max_slots=1)
    with pytest.raises(ConcurrencyError):
        repo.create(_grid(status=ACTIVE), max_slots=1)


def test_transition_optimistic_lock_and_slot_release(store):
    repo = _repo(store)
    g = repo.create(_grid(status=OPENING))
    # 陈旧 version 抛 ConcurrencyError
    with pytest.raises(ConcurrencyError):
        repo.transition_status(g.id, ACTIVE, expected_version=999)
    g2 = repo.transition_status(g.id, ACTIVE, expected_version=g.version)
    assert g2.status == ACTIVE and g2.version == g.version + 1
    # 关到终态释放槽位，可再开同币种活跃网格
    repo.transition_status(g2.id, CLOSING, expected_version=g2.version)
    g4 = repo.get(g.id)
    repo.transition_status(g4.id, CLOSED, expected_version=g4.version)
    assert repo.get_active_by_symbol('okx', 'BTC/USDT:USDT') is None
    again = repo.create(_grid(status=ACTIVE))
    assert again.id != g.id


def test_illegal_transition_raises_state_error(store):
    repo = _repo(store)
    g = repo.create(_grid(status=ACTIVE))
    with pytest.raises(StateError):
        repo.transition_status(g.id, PENDING, expected_version=g.version)


def test_list_active_excludes_terminal(store):
    repo = _repo(store)
    a = repo.create(_grid(symbol='AAA/USDT:USDT', status=ACTIVE))
    b = repo.create(_grid(symbol='BBB/USDT:USDT', status=OPENING))
    c = repo.create(_grid(symbol='CCC/USDT:USDT', status=ACTIVE))
    repo.transition_status(c.id, CLOSING, expected_version=c.version)
    c2 = repo.get(c.id)
    repo.transition_status(c2.id, CLOSED, expected_version=c2.version)
    ids = {g.id for g in repo.list_active()}
    assert a.id in ids and b.id in ids and c.id not in ids
