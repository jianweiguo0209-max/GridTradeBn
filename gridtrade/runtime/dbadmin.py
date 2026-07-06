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


def slotify_active_symbol(store) -> str:
    """幂等：旧格式 active_symbol='SYM' 改写为槽位格式 'SYM#0'（cap=2 槽位方案，
    spec 2026-07-06-tiered-*；UNIQUE(exchange,active_symbol) 不变，'||' 拼接 PG/SQLite 通用）。"""
    with store.engine.begin() as c:
        res = c.execute(sa.text(
            "UPDATE grids SET active_symbol = active_symbol || '#0' "
            "WHERE active_symbol IS NOT NULL AND active_symbol NOT LIKE '%#%'"))
    return ('updated %d' % res.rowcount) if res.rowcount else 'skipped'


def migrate(store) -> list:
    """跑所有增量迁移（幂等）。返回每步结果。"""
    return [('add_grid_fills_fee', add_grid_fills_fee(store)),
            ('add_grids_fuse_oids', add_grids_fuse_oids(store)),
            ('slotify_active_symbol', slotify_active_symbol(store))]


def validate_1m_cache(cache, *, dry_run=False, warm_fn=None, log=print):
    """扫全 1m 缓存 → 分类 → 坏格聚合成天 → warm 重取（dry_run 时只报告）。幂等。
    返回 {scanned, ok, range_mismatch, hour_gap, no_1h_ref, refetched_days, still_bad}。"""
    import pandas as pd
    from gridtrade.backtest.reservoir import validate_1m_cell, warm_reservoir_ohlcv
    warm_fn = warm_fn or warm_reservoir_ohlcv
    rep = {'scanned': 0, 'ok': 0, 'range_mismatch': 0, 'hour_gap': 0,
           'no_1h_ref': 0, 'refetched_days': 0, 'still_bad': 0}
    bad_by_day = {}
    for sym in cache.list_symbols('1m'):
        for day in cache.list_days('1m', sym):
            rep['scanned'] += 1
            ok, reason = validate_1m_cell(cache.read('1m', sym, day),
                                          cache.read('1h', sym, day))
            rep[reason] = rep.get(reason, 0) + 1
            if not ok:
                bad_by_day.setdefault(day, set()).add(sym)
    if dry_run:
        log('[validate-1m] DRY scanned=%d ok=%d range=%d gap=%d no1h=%d 坏天=%d'
            % (rep['scanned'], rep['ok'], rep['range_mismatch'], rep['hour_gap'],
               rep['no_1h_ref'], len(bad_by_day)))
        return rep
    for day, syms in sorted(bad_by_day.items()):
        s_ms = int(pd.Timestamp(day).value // 1_000_000)
        e_ms = s_ms + 86_400_000 - 1
        warm_fn(cache, sorted(syms), s_ms, e_ms, log=log)
        rep['refetched_days'] += 1
        for sym in syms:
            ok, _ = validate_1m_cell(cache.read('1m', sym, day),
                                     cache.read('1h', sym, day))
            if not ok:
                rep['still_bad'] += 1
    log('[validate-1m] scanned=%d refetched_days=%d still_bad=%d'
        % (rep['scanned'], rep['refetched_days'], rep['still_bad']))
    return rep


def run(action, *, store_factory=None):
    if action == 'validate-1m':      # 缓存维护，不需要 DB store
        import os
        from gridtrade.backtest.cache import ParquetCache
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', '..', 'data', 'hl_validate')
        return validate_1m_cache(ParquetCache(root), dry_run='--dry-run' in sys.argv)
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
    raise SystemExit('usage: python -m gridtrade.runtime.dbadmin '
                     '[create|reset|migrate|validate-1m [--dry-run]]')


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'create'
    result = run(action)
    print('[dbadmin] %s done: %s' % (action, result), flush=True)


if __name__ == '__main__':
    main()
