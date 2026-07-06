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


def test_slotify_active_symbol_idempotent(store):
    # 槽位迁移：旧格式 'SYM' → 'SYM#0'；已槽位化/NULL 不动；重跑 skipped。
    import sqlalchemy as sa
    from gridtrade.runtime.dbadmin import slotify_active_symbol
    with store.engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO grids (id, exchange, symbol, status, offset, tag, direction,"
            " active_symbol, created_at, updated_at, version)"
            " VALUES ('m1', 'hl', 'BTC/USDC:USDC', 'ACTIVE', 0, 't', 'neutral',"
            " 'BTC/USDC:USDC', 0, 0, 1)"))
        c.execute(sa.text(
            "INSERT INTO grids (id, exchange, symbol, status, offset, tag, direction,"
            " active_symbol, created_at, updated_at, version)"
            " VALUES ('m2', 'hl', 'ETH/USDC:USDC', 'CLOSED', 0, 't', 'neutral',"
            " NULL, 0, 0, 1)"))
    out = slotify_active_symbol(store)
    assert out.startswith('updated 1')
    with store.engine.connect() as c:
        v = c.execute(sa.text("SELECT active_symbol FROM grids WHERE id='m1'")).scalar()
        assert v == 'BTC/USDC:USDC#0'
        assert c.execute(sa.text("SELECT active_symbol FROM grids WHERE id='m2'")).scalar() is None
    assert slotify_active_symbol(store) == 'skipped'        # 幂等
