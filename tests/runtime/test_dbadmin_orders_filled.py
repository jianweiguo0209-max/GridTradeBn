# tests/runtime/test_dbadmin_orders_filled.py
"""迁移:grid_orders 加 filled 列(spec 2026-07-09-partial-fill-lifecycle)。
存量行 default 0 正确——旧代码下部分成交立即 closed,不存在 open 且已部分摄入的行。"""
import sqlalchemy as sa

from gridtrade.runtime.dbadmin import add_grid_orders_filled
from gridtrade.state.store import StateStore


def _legacy_orders_table(engine):
    md = sa.MetaData()
    sa.Table(
        'grid_orders', md,
        sa.Column('client_oid', sa.String, primary_key=True),
        sa.Column('grid_id', sa.String, nullable=False),
        sa.Column('line_index', sa.Integer, nullable=False),
        sa.Column('exchange_order_id', sa.String, nullable=True),
        sa.Column('side', sa.String, nullable=False),
        sa.Column('price', sa.Float, nullable=False),
        sa.Column('size', sa.Float, nullable=False),
        sa.Column('status', sa.String, nullable=False),
        sa.Column('created_at', sa.BigInteger, nullable=False),
        sa.Column('updated_at', sa.BigInteger, nullable=False),
    )
    md.create_all(engine)


def _cols(engine):
    return {c['name'] for c in sa.inspect(engine).get_columns('grid_orders')}


def test_migrate_adds_filled_column():
    st = StateStore.in_memory()
    _legacy_orders_table(st.engine)
    assert 'filled' not in _cols(st.engine)
    assert add_grid_orders_filled(st) == 'added'
    assert 'filled' in _cols(st.engine)


def test_migrate_idempotent_on_fresh_db():
    st = StateStore.in_memory()
    st.create_all()
    assert add_grid_orders_filled(st) == 'skipped'
