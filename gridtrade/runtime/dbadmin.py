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


def add_grids_close_reason(store) -> str:
    """幂等：grids 缺 close_reason 列则加上（NULL 允许）——关格真因持久化。"""
    cols = {c['name'] for c in sa.inspect(store.engine).get_columns('grids')}
    if 'close_reason' in cols:
        return 'skipped'
    with store.engine.begin() as c:
        c.execute(sa.text('ALTER TABLE grids ADD COLUMN close_reason VARCHAR'))
    return 'added'


def slotify_active_symbol(store) -> str:
    """幂等：旧格式 active_symbol='SYM' 改写为槽位格式 'SYM#0'（cap=2 槽位方案，
    spec 2026-07-06-tiered-*；UNIQUE(exchange,active_symbol) 不变，'||' 拼接 PG/SQLite 通用）。"""
    with store.engine.begin() as c:
        res = c.execute(sa.text(
            "UPDATE grids SET active_symbol = active_symbol || '#0' "
            "WHERE active_symbol IS NOT NULL AND active_symbol NOT LIKE '%#%'"))
    return ('updated %d' % res.rowcount) if res.rowcount else 'skipped'


def add_grid_orders_filled(store) -> str:
    """幂等：grid_orders 缺 filled 列则加上（DEFAULT 0）——部分成交生命周期
    (spec 2026-07-09)。存量行 0 正确：旧代码下部分成交立即 closed，
    不存在 open 且已部分摄入的行。"""
    cols = {c['name'] for c in sa.inspect(store.engine).get_columns('grid_orders')}
    if 'filled' in cols:
        return 'skipped'
    with store.engine.begin() as c:
        c.execute(sa.text(
            'ALTER TABLE grid_orders ADD COLUMN filled DOUBLE PRECISION NOT NULL DEFAULT 0'))
    return 'added'


def verify_ledger(store, adapter=None, log=print, records=False) -> dict:
    """组件三(spec 2026-07-11-symbol-desk):合成行守恒审计,只读幂等。
    ①转仓对按共享 eid 配对:恰 2 行/带符号量和≈0/同价(masking 唯一来源=合成行写错,
      配对守恒使其必留痕);②`ledger:reduce` 单边行按设计跳过;③旧 5 段格式计 legacy;
    ④每活跃格重放净仓(Σ signed fills) vs accounting 快照(容差 1.5×order_num,同
      drift-check 口径——审计与告警一个标尺);⑤adapter 给出时:per-symbol Σclaims vs
      交易所净仓;⑥records=True(spec 2026-07-12-honest-record-pnl 组件二):每条关格
      record 用 DB fills 直算重验(pnl_exact+funding vs total_pnl,容差 max($0.05,
      0.1%×cap))——引擎时代历史失真全量曝光。
    巡查定期跑:`python -m gridtrade.runtime.dbadmin verify-ledger [--records]`。"""
    from collections import defaultdict
    from gridtrade.state.grids import GridRepository
    from gridtrade.state.accounting import AccountingRepository
    from gridtrade.state.models import grid_fills
    rep = {'scanned': 0, 'pairs_ok': 0, 'pairs_bad': 0, 'legacy': 0,
           'replay_bad': 0, 'symbol_drift': 0}
    with store.engine.connect() as c:
        rows = c.execute(sa.select(grid_fills)).all()
    by_eid = defaultdict(list)
    for r in rows:
        m = r._mapping
        tid = m['trade_id']
        if not tid.startswith('ledger:'):
            continue
        rep['scanned'] += 1
        parts = tid.split(':')
        if len(parts) != 4 or '-' not in parts[3]:
            rep['legacy'] += 1               # 升级前旧格式(ts:seq 5 段):只读兼容,不配对
            continue
        event, eid = parts[1], parts[3]
        if event == 'reduce':
            continue                         # 单边 by design(有真实 reduce 市价单对应)
        by_eid[(event, eid)].append(m)
    for key, pair in sorted(by_eid.items()):
        signed = sum((1 if p['side'] == 'buy' else -1) * p['size'] for p in pair)
        prices = {p['price'] for p in pair}
        if len(pair) == 2 and abs(signed) < 1e-9 and len(prices) == 1:
            rep['pairs_ok'] += 1
        else:
            rep['pairs_bad'] += 1
            log('[verify-ledger] BAD pair %s: rows=%d signed=%.3g prices=%s'
                % (key, len(pair), signed, sorted(prices)))
    gr, ar = GridRepository(store), AccountingRepository(store)
    fills_by_grid = defaultdict(float)
    for r in rows:
        m = r._mapping
        fills_by_grid[m['grid_id']] += (1 if m['side'] == 'buy' else -1) * m['size']
    claims = {}
    for g in gr.list_active():
        acc = ar.get(g.id)
        if acc is None:
            continue
        replay = fills_by_grid.get(g.id, 0.0)
        claims.setdefault((g.exchange, g.symbol), 0.0)
        claims[(g.exchange, g.symbol)] += replay
        tol = 1.5 * float(g.order_num or 0.0) + 1e-9
        if abs(replay - float(acc.net_position or 0.0)) > tol:
            rep['replay_bad'] += 1
            log('[verify-ledger] REPLAY mismatch grid=%s %s replay=%.6g acc=%.6g tol=%.3g'
                % (g.id, g.symbol, replay, acc.net_position, tol))
    if adapter is not None:
        for (exch, sym), total in sorted(claims.items()):
            real = float(adapter.fetch_positions(sym).net_size)
            if abs(total - real) > 1e-6:
                rep['symbol_drift'] += 1
                log('[verify-ledger] SYMBOL drift %s claims=%.6g exchange=%.6g'
                    % (sym, total, real))
    if records:
        from gridtrade.execution.live_equity import LiveEquity
        from gridtrade.state.models import order_records
        rep.update({'records_scanned': 0, 'records_bad': 0, 'records_nofills': 0,
                    'records_openmark': 0})
        with store.engine.connect() as c:
            recs = c.execute(sa.select(order_records)).all()
        by_grid = defaultdict(list)
        for r in rows:
            by_grid[r._mapping['grid_id']].append(r._mapping)
        for r in recs:
            rec = r._mapping
            rep['records_scanned'] += 1
            fl = sorted(by_grid.get(rec['grid_id'], []),
                        key=lambda m: (m['ts'], m['trade_id']))
            if not fl:
                rep['records_nofills'] += 1     # 迁移前旧格无 fills:只计数不判
                continue
            cap = float(rec['sz'] or 0.0)
            le = LiveEquity(cap or 1.0)
            for m in fl:
                le.record_fill(m['price'], m['side'], m['size'], m['ts'],
                               float(m['fee'] or 0.0))
            acc = ar.get(rec['grid_id'])
            if acc is not None and acc.funding_paid:
                le.add_funding(float(acc.funding_paid))
            last_px = float(fl[-1]['price'])
            r_ex = le.pnl_exact(last_px)
            # 重放残留净仓 → record 的 pnl 依赖关格时刻 mark(离线不可得),不可判:
            # 老 _flatten_symbol 不落退出合成行的存量记录归此桶(2026-07-12 起补行)。
            if abs(r_ex['net']) * last_px > 1.0:
                rep['records_openmark'] += 1
                continue
            exact = r_ex['pnl']
            tol = max(0.05, 0.001 * abs(cap))
            if abs(exact - float(rec['total_pnl'] or 0.0)) > tol:
                rep['records_bad'] += 1
                log('[verify-ledger] RECORD deviation grid=%s %s tag=%s reason=%s '
                    'record=%+.4f exact=%+.4f Δ=%+.4f'
                    % (rec['grid_id'], rec['symbol'], rec['tag'], rec['exit_reason'],
                       float(rec['total_pnl'] or 0.0), exact,
                       exact - float(rec['total_pnl'] or 0.0)))
    log('[verify-ledger] scanned=%(scanned)d pairs_ok=%(pairs_ok)d pairs_bad=%(pairs_bad)d '
        'legacy=%(legacy)d replay_bad=%(replay_bad)d symbol_drift=%(symbol_drift)d' % rep
        + (' records_scanned=%(records_scanned)d records_bad=%(records_bad)d '
           'records_nofills=%(records_nofills)d records_openmark=%(records_openmark)d'
           % rep if records else ''))
    return rep


def migrate(store) -> list:
    """跑所有增量迁移（幂等）。返回每步结果。"""
    return [('add_grid_fills_fee', add_grid_fills_fee(store)),
            ('add_grids_fuse_oids', add_grids_fuse_oids(store)),
            ('add_grids_close_reason', add_grids_close_reason(store)),
            ('slotify_active_symbol', slotify_active_symbol(store)),
            ('add_grid_orders_filled', add_grid_orders_filled(store))]


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
    if action == 'verify-ledger':
        adapter = None
        if '--exchange' in sys.argv:
            from gridtrade.config import load_deploy_config
            from gridtrade.exchanges.registry import build_adapter
            cfg = load_deploy_config()
            adapter = build_adapter({'exchange': cfg.exchange,
                                     'api_key': cfg.api_key,
                                     'secret': cfg.api_secret,
                                     'testnet': cfg.testnet,
                                     'quote_currency': cfg.quote_currency})
        return verify_ledger(store, adapter=adapter, records='--records' in sys.argv)
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
                     '[create|reset|migrate|verify-ledger [--exchange] [--records]|validate-1m [--dry-run]]')


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'create'
    result = run(action)
    print('[dbadmin] %s done: %s' % (action, result), flush=True)


if __name__ == '__main__':
    main()
