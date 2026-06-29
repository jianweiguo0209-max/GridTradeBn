import sqlalchemy as sa

from gridtrade.runtime.dbadmin import add_grid_fills_fee
from gridtrade.state.store import StateStore


def _table_without_fee(engine):
    """建一个不含 fee 列的 grid_fills（模拟迁移前旧库）。"""
    md = sa.MetaData()
    sa.Table(
        'grid_fills', md,
        sa.Column('trade_id', sa.String, primary_key=True),
        sa.Column('grid_id', sa.String, nullable=False),
        sa.Column('line_index', sa.Integer, nullable=False),
        sa.Column('side', sa.String, nullable=False),
        sa.Column('price', sa.Float, nullable=False),
        sa.Column('size', sa.Float, nullable=False),
        sa.Column('ts', sa.BigInteger, nullable=False),
        sa.Column('created_at', sa.BigInteger, nullable=False),
    )
    md.create_all(engine)


def _cols(engine):
    return {c['name'] for c in sa.inspect(engine).get_columns('grid_fills')}


def test_migrate_adds_fee_column():
    st = StateStore.in_memory()
    _table_without_fee(st.engine)
    assert 'fee' not in _cols(st.engine)
    assert add_grid_fills_fee(st) == 'added'
    assert 'fee' in _cols(st.engine)


def test_migrate_is_idempotent():
    st = StateStore.in_memory()
    st.create_all()                       # 新库已含 fee 列（Task 1 后）
    assert add_grid_fills_fee(st) == 'skipped'
    assert add_grid_fills_fee(st) == 'skipped'
