import sqlalchemy as sa
import pytest


def test_create_all_builds_tables(store):
    insp = sa.inspect(store.engine)
    tables = set(insp.get_table_names())
    assert {'grids', 'grid_orders', 'grid_accounting', 'order_records'} <= tables


def test_active_symbol_unique_blocks_second_active(store):
    from gridtrade.state.models import grids
    row = dict(id='g1', exchange='okx', symbol='BTC/USDT:USDT',
               active_symbol='BTC/USDT:USDT', offset=0, tag='t', status='ACTIVE',
               direction='neutral', created_at=1, updated_at=1, version=1)
    with store.engine.begin() as c:
        c.execute(sa.insert(grids), row)
    with pytest.raises(sa.exc.IntegrityError):
        with store.engine.begin() as c:
            c.execute(sa.insert(grids), dict(row, id='g2'))


def test_null_active_symbol_does_not_collide(store):
    from gridtrade.state.models import grids
    base = dict(exchange='okx', symbol='BTC/USDT:USDT', active_symbol=None,
                offset=0, tag='t', status='CLOSED', direction='neutral',
                created_at=1, updated_at=1, version=1)
    with store.engine.begin() as c:
        c.execute(sa.insert(grids), dict(base, id='g3'))
        c.execute(sa.insert(grids), dict(base, id='g4'))
    with store.engine.begin() as c:
        n = c.execute(sa.select(sa.func.count()).select_from(grids)).scalar()
    assert n == 2


def test_can_transition_and_states():
    from gridtrade.state import models as m
    assert m.can_transition(m.PENDING, m.OPENING)
    assert m.can_transition(m.OPENING, m.ACTIVE)
    assert m.can_transition(m.ACTIVE, m.CLOSING)
    assert m.can_transition(m.CLOSING, m.CLOSED)
    assert not m.can_transition(m.ACTIVE, m.PENDING)
    assert not m.can_transition(m.CLOSED, m.ACTIVE)
    assert set(m.ACTIVE_STATES) == {m.PENDING, m.OPENING, m.ACTIVE, m.CLOSING}
