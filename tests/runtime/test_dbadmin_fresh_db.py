"""锁定 mainnet 首部署所依赖的空库行为：裸 migrate 在空库上抛错（NoSuchTableError），
先 create（create_all，按当前模型建含 fee/fuse 列的全表）后 migrate 变幂等 no-op。
这是 deploy/fly.prod.toml 用 `create && migrate` 而非裸 `migrate` 的根据。"""
import pytest
import sqlalchemy as sa

from gridtrade.runtime.dbadmin import migrate, run
from gridtrade.state.store import StateStore


def test_bare_migrate_on_empty_db_raises():
    st = StateStore.in_memory()          # 全新空库，未 create_all
    with pytest.raises(sa.exc.NoSuchTableError):
        migrate(st)


def test_create_then_migrate_on_empty_db_is_clean():
    st = StateStore.in_memory()
    assert run('create', store_factory=lambda: st) == 'create'
    # create_all 已按当前模型建好含 fee/fuse 列的表 → migrate 全部 skipped、不抛错
    results = run('migrate', store_factory=lambda: st)
    assert results == [('add_grid_fills_fee', 'skipped'),
                       ('add_grids_fuse_oids', 'skipped')]
