import pytest

from gridtrade.runtime import dbadmin
from gridtrade.state.grids import GridRepository
from gridtrade.state.models import Grid, PENDING


def test_dbadmin_reset_drops_then_recreates_empty(store):
    GridRepository(store).create(Grid(id='', exchange='okx', symbol='X', status=PENDING))
    assert GridRepository(store).list_active()           # 有数据
    res = dbadmin.run('reset', store_factory=lambda: store)
    assert res == 'reset'
    assert GridRepository(store).list_active() == []     # drop+create 后清空、表仍在


def test_dbadmin_create_is_idempotent(store):
    assert dbadmin.run('create', store_factory=lambda: store) == 'create'
    assert dbadmin.run('create', store_factory=lambda: store) == 'create'  # 再来不报错


def test_dbadmin_unknown_action_raises(store):
    with pytest.raises(SystemExit):
        dbadmin.run('nope', store_factory=lambda: store)
