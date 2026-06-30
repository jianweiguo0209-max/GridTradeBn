"""DB 管理一次性入口：create / reset / migrate。在 fly 上用 `fly machine run <image> \
python -m gridtrade.runtime.dbadmin <action>` 跑一次。

- create：仅 create_all（幂等，安全）。
- reset：drop_all + create_all（**销毁所有表数据**，仅 testnet/无价值数据时用）。
- migrate：对已存在的库做增量迁移（幂等）。当前：grid_fills 加 fee 列。
"""
import sys

import sqlalchemy as sa

from gridtrade.config import load_deploy_config
from gridtrade.state.store import StateStore


def _store():
    cfg = load_deploy_config()
    return (StateStore.from_url(cfg.database_url) if cfg.database_url
            else StateStore.in_memory())


def add_grid_fills_fee(store) -> str:
    """幂等：grid_fills 缺 fee 列则加上（DEFAULT 0），有则跳过。返回 'added'/'skipped'。"""
    cols = {c['name'] for c in sa.inspect(store.engine).get_columns('grid_fills')}
    if 'fee' in cols:
        return 'skipped'
    with store.engine.begin() as c:
        c.execute(sa.text(
            'ALTER TABLE grid_fills ADD COLUMN fee DOUBLE PRECISION NOT NULL DEFAULT 0'))
    return 'added'


def add_grids_fuse_oids(store) -> str:
    """幂等：grids 缺 fuse_low_oid/fuse_high_oid 列则加上（NULL 允许）。"""
    cols = {c['name'] for c in sa.inspect(store.engine).get_columns('grids')}
    todo = [c for c in ('fuse_low_oid', 'fuse_high_oid') if c not in cols]
    if not todo:
        return 'skipped'
    with store.engine.begin() as c:
        for col in todo:
            c.execute(sa.text('ALTER TABLE grids ADD COLUMN %s VARCHAR' % col))
    return 'added'


def migrate(store) -> list:
    """跑所有增量迁移（幂等）。返回每步结果。"""
    return [('add_grid_fills_fee', add_grid_fills_fee(store)),
            ('add_grids_fuse_oids', add_grids_fuse_oids(store))]


def run(action, *, store_factory=None):
    store = store_factory() if store_factory else _store()
    if action == 'reset':
        store.drop_all()
        store.create_all()
        return 'reset'
    if action == 'create':
        store.create_all()
        return 'create'
    if action == 'migrate':
        return migrate(store)
    raise SystemExit('usage: python -m gridtrade.runtime.dbadmin [create|reset|migrate]')


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'create'
    result = run(action)
    print('[dbadmin] %s done: %s' % (action, result), flush=True)


if __name__ == '__main__':
    main()
