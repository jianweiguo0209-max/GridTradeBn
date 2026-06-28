import pytest

from gridtrade.runtime import dbadmin
from gridtrade.state.store import StateStore
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import Grid, PENDING


def test_dbadmin_reset_drops_then_recreates_empty():
    s = StateStore.in_memory()
    s.create_all()
    GridRepository(s).create(Grid(id='', exchange='okx', symbol='X', status=PENDING))
    assert GridRepository(s).list_active()           # 有数据
    res = dbadmin.run('reset', store_factory=lambda: s)
    assert res == 'reset'
    assert GridRepository(s).list_active() == []     # drop+create 后清空、表仍在


def test_dbadmin_create_is_idempotent():
    s = StateStore.in_memory()
    assert dbadmin.run('create', store_factory=lambda: s) == 'create'
    assert dbadmin.run('create', store_factory=lambda: s) == 'create'  # 再来不报错


def test_dbadmin_unknown_action_raises():
    with pytest.raises(SystemExit):
        dbadmin.run('nope', store_factory=lambda: StateStore.in_memory())
